import math
from collections import Counter
import json
from pathlib import Path
from fastapi import APIRouter,Depends,HTTPException,Query
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import func,text
from sqlalchemy.orm import Session
from .db import get_db
from .config import get_settings
from .models import User,Area,Report,Advisory,Audit,Outbox,LoginSession,now
from .schemas import UserCreate,UserAccess,AreaCreate
from .security import current_user,require_role,hash_password,audit,is_case_staff
from .reports import visible_query,report_view,serialize_report
from .auth import user_view
from . import ai
from . import spatial
from .cache import cache,touch
from .osm_areas import _parse_bbox

router=APIRouter(tags=['Workspace, maps, administration'])

def _alembic_config():
    cfg=Config(str(Path(__file__).resolve().parents[1] / 'alembic.ini'))
    cfg.set_main_option('script_location', str(Path(__file__).resolve().parents[1] / 'alembic'))
    return cfg

def _alembic_head():
    return ScriptDirectory.from_config(_alembic_config()).get_current_head()

def _alembic_current(db):
    try:
        row=db.execute(text('SELECT version_num FROM alembic_version')).first()
    except Exception:
        return None
    return row[0] if row else None

def _postgis_version(db):
    try:
        row=db.execute(text("SELECT extversion FROM pg_extension WHERE extname='postgis'")).first()
    except Exception as exc:
        raise HTTPException(503, 'Unable to inspect PostGIS capability.') from exc
    return row[0] if row else None

def database_capabilities(db):
    if db.bind.dialect.name!='postgresql':
        return {'status':'ok','database':'sqlite-local-development','alembic':{'current':None,'head':None}}
    current=_alembic_current(db)
    head=_alembic_head()
    postgis=_postgis_version(db)
    database='postgresql-postgis' if postgis else 'postgresql'
    payload={'status':'ok','database':database,'postgis_version':postgis,'alembic':{'current':current,'head':head}}
    if current!=head:
        raise HTTPException(503, {'status':'error','database':database,'postgis_version':postgis,
            'alembic':{'current':current,'head':head},'detail':'Database schema is not at the expected Alembic head.'})
    return payload

@router.get('/health/live')
def live():return {'status':'ok','service':'EcoGuard API'}

@router.get('/health/ready')
def ready(db:Session=Depends(get_db)):
    db.execute(text('SELECT 1'))
    return database_capabilities(db)

@router.get('/config')
def config():
    cfg=get_settings()
    # Only the deployment's static fields are cached. The image-assistance state is read
    # live on every call: a cached 'degraded' would hide a model that has just become
    # ready, and a cached 'ready' would hide one that has failed.
    body=cache.cached('config',cfg.cache_ttl_seconds,lambda:
        {'name':'EcoGuard Uganda','demo_enabled':cfg.demo_enabled,'application_only':True,
        'sms':cfg.sms_provider,'max_upload_mb':cfg.max_upload_mb,'languages':['en'],
        'osm_attribution':cfg.osm_attribution,'osm_source_name':cfg.osm_source_name})
    ai_state=ai.status()
    return {**body,'image_assistance':ai_state['state'],'ai_model_version':ai_state['model_version']}

@router.get('/areas')
def areas(db:Session=Depends(get_db)):
    cfg=get_settings()
    # Key carries the 'areas:' prefix that cache.touch() invalidates, so a newly created
    # area appears immediately instead of waiting out the TTL.
    return cache.cached('areas:',cfg.cache_ttl_seconds,lambda:
        {'items':[{'id':a.id,'name':a.name,'description':a.description,'latitude':a.latitude,
        'longitude':a.longitude,'radius_km':a.radius_km} for a in db.query(Area).order_by(Area.name).all()]})

@router.get('/dashboard')
def dashboard(user:User=Depends(current_user),db:Session=Depends(get_db)):
    cfg=get_settings()
    return cache.cached('dashboard:'+user.id,cfg.cache_ttl_seconds,lambda:_dashboard_rows(db,user))

def _dashboard_rows(db,user):
    rows=visible_query(db,user).all()
    modes=spatial.visibility_map(db,user,rows)
    categories=Counter(r.category for r in rows);states=Counter(r.state for r in rows)
    days=Counter(r.created_at[:10] for r in rows)
    return {'counts':{'wildlife':categories['wildlife'],'wetland':categories['wetland'],'flood':categories['flood'],
        'awaiting_review':sum(states[k] for k in ('submitted','under_review','needs_evidence')),'total':len(rows),
        'verified':states['verified'],'closed':states['closed']},
        'states':dict(states),'categories':dict(categories),
        'activity':[{'date':day,'count':count} for day,count in sorted(days.items())[-30:]],
        'recent':[serialize_report(db,r,user,modes.get(r.id,'full')) for r in sorted(rows,key=lambda r:r.created_at,reverse=True)[:6]],
        'scope':'assigned areas and own reports' if is_case_staff(user) else 'your reports',
        'ai_metrics':None,'note':'Operational counts are not field-impact or model-accuracy measurements.'}

# Default country viewport (Uganda); the client normally sends the live map bounds.
DEFAULT_BBOX = (29.0, -1.5, 35.5, 4.5)
# Point features are aggregated into viewport clusters below this zoom; at or
# above it the individual (already-generalised) points are returned.
CLUSTER_ZOOM = 10
CLUSTER_BASE_CELL = 0.09  # degrees at zoom 9; doubles per zoom level below that


@router.get('/map')
def map_data(view: str = Query('community', pattern='^(community|staff)$'),
             category: str | None = None,
             bbox: str | None = Query(None, description='minLon,minLat,maxLon,maxLat'),
             zoom: int | None = Query(None, ge=0, le=22),
             user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Viewport-scoped map data with low-zoom clustering.

    The client sends the current viewport (``bbox``, ``zoom``); the server
    filters spatially instead of applying the old hard 1000-feature cap, and
    aggregates points into clusters at low zooms so the payload stays small.
    Staff responses are cached per user (the full/redacted split depends on
    their assignments); everything carries a cache key built from
    view + bbox + zoom + category.
    """
    cfg = get_settings()
    minx, miny, maxx, maxy = DEFAULT_BBOX
    if bbox:
        minx, miny, maxx, maxy = _parse_bbox(bbox)
    z = zoom if zoom is not None else (8 if view == 'staff' else 7)
    rbox = f'{minx:.3f},{miny:.3f},{maxx:.3f},{maxy:.3f}'
    if view == 'staff':
        # Admins are included deliberately: excluding them hid staff cases from the
        # operators who manage access. This exposes other people's report titles and,
        # for shared locations, private positions -- but only within assigned areas.
        require_role(user, 'reviewer', 'responder', 'publisher', 'admin')
        key = f'map:staff:{user.id}:{category or "all"}:z{z}:{rbox}'
        body = cache.cached(key, cfg.cache_ttl_seconds,
                            lambda: _staff_map(db, user, category, minx, miny, maxx, maxy, z))
    else:
        # Public output is built only from active, published advisories, not
        # hidden reports. Cache is view-scoped and shared, like the old key.
        key = f'map:community:{category or "all"}:z{z}:{rbox}'
        body = cache.cached(key, cfg.cache_ttl_seconds,
                            lambda: _community_map(db, category, minx, miny, maxx, maxy, z))
    debug = dict(body.get('debug') or {})
    debug['cache_key'] = key
    return {**body, 'debug': debug}


def _map_body(features, clusters, areas, source):
    return {'type': 'FeatureCollection', 'features': features, 'clusters': clusters,
            'areas': areas,
            'location_policy': 'Private positions are never included in community responses.',
            'debug': {'source': source, 'clustered': bool(clusters)}}


def _staff_map(db, user, category, minx, miny, maxx, maxy, zoom):
    if spatial.postgis_active(db):
        ids = _report_ids_in_bbox(db, category, minx, miny, maxx, maxy)
        if not ids:
            return _map_body([], [], [], 'db')
        query = db.query(Report).filter(Report.id.in_(ids))
        if category:
            query = query.filter(Report.category == category)
        rows = query.all()
    else:
        # SQLite harness: no spatial filtering, keep the legacy limit + gating.
        query = visible_query(db, user)
        if category:
            query = query.filter(Report.category == category)
        rows = query.limit(1000).all()
    modes = spatial.visibility_map(db, user, rows)
    features, feature_ids = [], []
    for r in rows:
        area = db.get(Area, r.area_id)
        if modes.get(r.id, 'full') == 'redacted':
            # Out-of-area staff see only the generalised point - no title,
            # case code or precise coordinates can leak. (If the report has no
            # stored public point its legacy area centroid stands in.)
            lon, lat = (r.longitude if r.longitude is not None else area.longitude,
                        r.latitude if r.latitude is not None else area.latitude)
            features.append({'type': 'Feature', 'id': r.id,
                             'geometry': {'type': 'Point', 'coordinates': [lon, lat]},
                             'properties': {'id': r.id, 'category': r.category, 'state': r.state,
                                            'area_name': area.name, 'precision': 'generalised',
                                            'kind': 'report', 'redacted': True}})
        else:
            private = r.share_location and r.latitude is not None
            lon, lat = (r.longitude, r.latitude) if private else (area.longitude, area.latitude)
            features.append({'type': 'Feature', 'id': r.id,
                             'geometry': {'type': 'Point', 'coordinates': [lon, lat]},
                             'properties': {'id': r.id, 'code': r.code, 'title': r.title,
                                            'category': r.category, 'state': r.state,
                                            'area_name': area.name,
                                            'precision': 'private-evidence' if private else 'community-centroid',
                                            'kind': 'report'}})
        feature_ids.append(r.id)
    if spatial.postgis_active(db):
        features, clusters = _maybe_cluster(features, zoom)
        areas = _overlay_areas(db, user, feature_ids, minx, miny, maxx, maxy)
    else:
        clusters, areas = [], []
    return _map_body(features, clusters, areas, 'db')


def _report_ids_in_bbox(db, category, minx, miny, maxx, maxy):
    """Reports whose *displayed* point (private precise point, else legacy
    centroid) intersects the viewport. Preserves the existing private-evidence
    vs community-centroid mapping: what gets exposed is unchanged, only the
    fetch is viewport-scoped."""
    if not spatial.postgis_active(db):
        return None
    sql = (
        'SELECT r.id FROM reports r JOIN areas a ON a.id = r.area_id '
        'WHERE ST_Intersects(ST_SetSRID(ST_MakePoint('
        '  CASE WHEN r.share_location AND r.latitude IS NOT NULL THEN r.longitude ELSE a.longitude END,'
        '  CASE WHEN r.share_location AND r.latitude IS NOT NULL THEN r.latitude ELSE a.latitude END), 4326),'
        '  ST_MakeEnvelope(:minx, :miny, :maxx, :maxy, 4326))')
    params = {'minx': minx, 'miny': miny, 'maxx': maxx, 'maxy': maxy}
    if category:
        sql += ' AND r.category = :category'
        params['category'] = category
    return [row[0] for row in db.execute(text(sql), params).all()]


def _community_map(db, category, minx, miny, maxx, maxy, zoom):
    if spatial.postgis_active(db):
        ids = _advisory_ids_in_bbox(db, category, minx, miny, maxx, maxy)
        query = db.query(Advisory).filter(Advisory.id.in_(ids)) if ids else db.query(Advisory).filter(text('1 = 0'))
        if category:
            query = query.filter(Advisory.category == category)
        rows = query.limit(1000).all()
    else:
        query = db.query(Advisory).filter(Advisory.state == 'published', Advisory.expires_at > now())
        if category:
            query = query.filter(Advisory.category == category)
        rows = query.limit(1000).all()
    features = []
    for a in rows:
        area = db.get(Area, a.area_id)
        features.append({'type': 'Feature', 'id': a.id,
                         'geometry': {'type': 'Point', 'coordinates': [area.longitude, area.latitude]},
                         'properties': {'id': a.id, 'title': a.title, 'category': a.category,
                                        'state': 'published', 'area_name': area.name,
                                        'precision': 'community-centroid', 'kind': 'advisory'}})
    features, clusters = _maybe_cluster(features, zoom)
    return _map_body(features, clusters, [], 'db')


def _advisory_ids_in_bbox(db, category, minx, miny, maxx, maxy):
    """Published, unexpired advisories whose displayed point (the legacy area
    centroid, matching the public marker) intersects the viewport."""
    if not spatial.postgis_active(db):
        return None
    sql = (
        'SELECT a.id FROM advisories a JOIN areas ar ON ar.id = a.area_id '
        'WHERE a.state = :published AND a.expires_at > :cutoff '
        'AND ST_Intersects(ST_SetSRID(ST_MakePoint(ar.longitude, ar.latitude), 4326),'
        '  ST_MakeEnvelope(:minx, :miny, :maxx, :maxy, 4326))')
    # expires_at is an ISO-8601 string column (legacy), so the "not expired"
    # comparison is string-vs-string, the same one the ORM path runs.
    params = {'published': 'published', 'cutoff': now(),
              'minx': minx, 'miny': miny, 'maxx': maxx, 'maxy': maxy}
    if category:
        sql += ' AND a.category = :category'
        params['category'] = category
    return [row[0] for row in db.execute(text(sql), params).all()]


def _overlay_areas(db, user, report_ids, minx, miny, maxx, maxy):
    """Boundary polygons for the map: the viewing staff member's assigned areas
    in the viewport (solid emphasis) plus the OSM areas containing the visible
    reports (muted/dashed when outside the assignment)."""
    if not spatial.postgis_active(db):
        return []
    layers: dict[int, dict] = {}
    rows = db.execute(text(
        'SELECT DISTINCT a.id, a.name, a.display_name, a.area_type,'
        '       ST_AsGeoJSON(a.simplified_geom) AS g '
        'FROM areas_osm a JOIN user_area_assignments u ON u.area_osm_id = a.id '
        'WHERE u.user_id = :uid AND u.revoked_at IS NULL AND a.active '
        'AND ST_Intersects(a.geom, ST_MakeEnvelope(:minx, :miny, :maxx, :maxy, 4326))'),
        {'uid': user.id, 'minx': minx, 'miny': miny, 'maxx': maxx, 'maxy': maxy}).mappings().all()
    for r in rows:
        layers[r['id']] = {'assigned': True, 'name': r['name'], 'display_name': r['display_name'],
                           'area_type': r['area_type'], 'geom': r['g']}
    if report_ids:
        rows = db.execute(text(
            'SELECT DISTINCT a.id, a.name, a.display_name, a.area_type,'
            '       ST_AsGeoJSON(a.simplified_geom) AS g '
            'FROM report_areas ra JOIN areas_osm a ON a.id = ra.area_osm_id '
            'WHERE ra.report_id = ANY(:ids) AND a.active'),
            {'ids': report_ids}).mappings().all()
        for r in rows:
            layers.setdefault(r['id'], {'assigned': False, 'name': r['name'],
                                        'display_name': r['display_name'],
                                        'area_type': r['area_type'], 'geom': r['g']})
    items = []
    for aid, info in layers.items():
        items.append({
            'id': aid,
            'name': info['name'] or info['display_name'] or f'OSM area {aid}',
            'area_type': info['area_type'],
            'assigned': info['assigned'],
            'geometry': json.loads(info['geom']) if info.get('geom') else None,
        })
    # Payload guard: a whole-country viewport can cover many polygons; the
    # simplified geometry is capped so the overlay never dominates the payload.
    return items[:60]


def _cluster_cell(zoom: int) -> float:
    return CLUSTER_BASE_CELL * (2 ** max(0, CLUSTER_ZOOM - zoom - 1))


def _maybe_cluster(features, zoom: int | None):
    """Aggregate dense point cells into count clusters below CLUSTER_ZOOM.

    Cluster centroids are snapped to the public generalisation grid so an
    aggregate can never expose a finer position than its source points.
    """
    if not features or zoom is None or zoom >= CLUSTER_ZOOM:
        return features, []
    cell = _cluster_cell(zoom)
    buckets: dict[tuple[int, int], list] = {}
    for f in features:
        lon, lat = f['geometry']['coordinates']
        buckets.setdefault((math.floor(lon / cell), math.floor(lat / cell)), []).append(f)
    clusters, kept = [], []
    for (cx, cy), items in buckets.items():
        if len(items) == 1:
            kept.append(items[0])
            continue
        slon = sum(f['geometry']['coordinates'][0] for f in items) / len(items)
        slat = sum(f['geometry']['coordinates'][1] for f in items) / len(items)
        glon, glat = spatial.grid_point(slon, slat)
        cats = Counter(f['properties']['category'] for f in items)
        clusters.append({
            'type': 'Feature', 'id': f'cluster-{cx}-{cy}',
            'geometry': {'type': 'Point', 'coordinates': [glon, glat]},
            'properties': {'id': f'cluster-{cx}-{cy}', 'kind': 'cluster', 'count': len(items),
                           'category': cats.most_common(1)[0][0], 'categories': dict(cats),
                           'precision': 'clustered'}})
    return kept, clusters

@router.get('/team/directory')
def directory(user:User=Depends(current_user),db:Session=Depends(get_db)):
    require_role(user,'reviewer','responder','publisher','admin')
    if spatial.postgis_active(db):
        # Overlap is decided by live assignments (areas_osm ids). The legacy
        # User.areas mirror is a mix of legacy keys and OSM ids after the
        # migration, so it can no longer be compared across users.
        rows=db.execute(text(
            'SELECT DISTINCT u.id, u.name, u.roles::text AS roles_text '
            'FROM users u '
            'JOIN user_area_assignments mine ON mine.user_id = :me AND mine.revoked_at IS NULL '
            'JOIN user_area_assignments ua ON ua.user_id = u.id '
            '   AND ua.area_osm_id = mine.area_osm_id AND ua.revoked_at IS NULL '
            'JOIN areas_osm a ON a.id = ua.area_osm_id AND a.active '
            "WHERE u.active AND u.roles::jsonb ?| ARRAY['reviewer','responder','publisher'] "
            'ORDER BY u.name'), {'me': user.id}).mappings().all()
        return {'items':[{'id':r['id'],'name':r['name'],
            'roles':json.loads(r['roles_text']),
            'areas':list(spatial.active_area_ids(db,r['id']))} for r in rows]}
    return {'items':[{'id':u.id,'name':u.name,'roles':u.roles,'areas':u.areas} for u in db.query(User).filter_by(active=True).all()
        if set(u.areas).intersection(user.areas) and set(u.roles).intersection({'reviewer','responder','publisher'})]}

@router.get('/admin/dashboard')
def admin_dashboard(user:User=Depends(current_user),db:Session=Depends(get_db)):
    """System-wide view for access administrators.

    Deliberately never cached: an administrator who acts on a warning here (assign an
    area, retry a failed job) must see it reflected on the very next read, and a cached
    'everything is fine' would be the one wrong answer to give.
    """
    require_role(user,'admin')
    staff_roles={'reviewer','responder','publisher','admin'}
    people=db.query(User.id,User.roles,User.areas,User.active).all()
    roles=Counter(r for p in people for r in p.roles)
    areas=db.query(Area.id,Area.name).order_by(Area.name).all()
    case_staff=[p for p in people if set(p.roles)&staff_roles]
    active_staff=[p for p in case_staff if p.active]
    staffed={a for p in active_staff for a in p.areas}
    unstaffed=[{'id':a,'name':n} for a,n in areas if a not in staffed]
    reports={s:n for s,n in db.query(Report.state,func.count()).group_by(Report.state).all()}
    categories={c:n for c,n in db.query(Report.category,func.count()).group_by(Report.category).all()}
    advisories={s:n for s,n in db.query(Advisory.state,func.count()).group_by(Advisory.state).all()}
    job_states={s:n for s,n in db.query(Outbox.state,func.count()).group_by(Outbox.state).all()}
    failed=[{'id':j.id,'kind':j.kind,'attempts':j.attempts,'last_error':j.last_error,'created_at':j.created_at}
        for j in db.query(Outbox).filter_by(state='failed').order_by(Outbox.created_at.desc()).limit(20).all()]
    live_admins=[p for p in people if p.active and 'admin' in p.roles]
    idle_admins=[p.id for p in live_admins if not p.areas]
    area_less_staff=[p.id for p in active_staff if not p.areas]

    attention=[]
    if not live_admins:attention.append({'severity':'high','message':'No active administrator remains. Nobody can grant or revoke access.'})
    if not areas:attention.append({'severity':'high','message':'No community area exists. Reports cannot be filed until one is created.'})
    if unstaffed:attention.append({'severity':'medium','message':f'{len(unstaffed)} area(s) have no active staff assigned. Reports there cannot be reviewed.'})
    if area_less_staff:attention.append({'severity':'medium','message':f'{len(area_less_staff)} staff account(s) have no assigned area, so their staff views are empty.'})
    if idle_admins:attention.append({'severity':'low','message':'An administrator has no assigned area. Staff dashboards and maps will look empty for them.'})
    if failed:attention.append({'severity':'high','message':f'{job_states.get("failed",0)} background job(s) failed and will not retry.'})
    if not attention:attention.append({'severity':'ok','message':'No access or delivery problems detected.'})

    return {'users':{'total':len(people),'active':sum(1 for p in people if p.active),
            'inactive':sum(1 for p in people if not p.active),'by_role':dict(roles),
            'case_staff':len(case_staff),'admins':len(live_admins)},
        'areas':{'total':len(areas),'with_staff':len(areas)-len(unstaffed),'without_staff':unstaffed},
        'reports':{'total':sum(reports.values()),'by_state':reports,'by_category':categories},
        'advisories':{'total':sum(advisories.values()),'by_state':advisories},
        'jobs':{'total':sum(job_states.values()),'by_state':job_states,'failed':failed},
        'image_assistance':ai.status(),
        'attention':attention,'generated_at':now(),
        'note':'Account and delivery counts describe system state. They are not measures of field impact.'}

@router.get('/admin/users')
def users(user:User=Depends(current_user),db:Session=Depends(get_db)):
    require_role(user,'admin')
    return {'items':[user_view(u,db) for u in db.query(User).order_by(User.created_at).limit(1000).all()]}

@router.post('/admin/users',status_code=201)
def create_user(payload:UserCreate,user:User=Depends(current_user),db:Session=Depends(get_db)):
    require_role(user,'admin')
    if db.query(User).filter_by(email=str(payload.email).lower()).first():raise HTTPException(409,'An account with this email already exists.')
    check_areas(db,payload.areas)
    u=User(name=payload.name,email=str(payload.email).lower(),password_hash=hash_password(payload.password),
        roles=list(set(payload.roles)),areas=list(set(payload.areas)),preferences={})
    db.add(u);db.flush()
    if spatial.postgis_active(db):
        spatial.set_assignments(db,u.id,[int(a) for a in payload.areas],assigned_by=user.id)
    audit(db,user,'access.user_created',u.id);db.commit()
    return user_view(u, db)

def check_areas(db,ids):
    """Validate the ``areas`` payload.

    On PostgreSQL administrators assign ``areas_osm`` ids (they become
    ``user_area_assignments`` rows) and inactive/unknown ids are rejected; the
    SQLite harness validates the legacy area keys it still mirrors in User.areas.
    """
    if spatial.postgis_active(db):
        try:
            requested=[int(i) for i in ids]
        except (TypeError,ValueError):
            raise HTTPException(422,'Unknown or inactive assigned area.') from None
        valid=spatial.valid_assignment_ids(db,requested)
        if len(valid)!=len(set(requested)):
            raise HTTPException(422,'Unknown or inactive assigned area.')
        return
    valid={a.id for a in db.query(Area).all()}
    if not set(ids).issubset(valid):raise HTTPException(422,'Unknown assigned area.')

@router.post('/admin/areas',status_code=201)
def create_area(payload:AreaCreate,user:User=Depends(current_user),db:Session=Depends(get_db)):
    """Create a public community centroid. The stored point is the area centre only."""
    require_role(user,'admin')
    if db.get(Area,payload.id):raise HTTPException(409,'An area with this key already exists.')
    a=Area(id=payload.id,name=payload.name,description=payload.description,latitude=payload.latitude,
        longitude=payload.longitude,radius_km=payload.radius_km)
    db.add(a);db.flush();audit(db,user,'area.created',a.id);db.commit()
    touch('areas.updated',a.id)
    return {'id':a.id,'name':a.name,'description':a.description,'latitude':a.latitude,
        'longitude':a.longitude,'radius_km':a.radius_km}

@router.patch('/admin/users/{user_id}')
def access(user_id:str,payload:UserAccess,user:User=Depends(current_user),db:Session=Depends(get_db)):
    require_role(user,'admin')
    if user_id==user.id:raise HTTPException(403,'Ask a different administrator to change your own access.')
    u=db.get(User,user_id)
    if not u:raise HTTPException(404,'User not found.')
    check_areas(db,payload.areas)
    u.roles=list(set(payload.roles));u.areas=list(set(payload.areas));u.active=payload.active
    if spatial.postgis_active(db):
        # Live assignments now live in user_area_assignments (areas_osm ids);
        # User.areas is dual-written as a derived, deprecated mirror until the
        # legacy display path is retired.
        spatial.set_assignments(db,u.id,[int(a) for a in payload.areas],assigned_by=user.id)
    db.query(LoginSession).filter_by(user_id=u.id).delete(synchronize_session=False)
    audit(db,user,'access.updated_sessions_revoked',u.id);db.commit()
    return user_view(u, db)

@router.get('/my-areas')
def my_areas(user:User=Depends(current_user),db:Session=Depends(get_db)):
    """A staff member's own live area assignments with open-case load.

    The legacy ``user.areas`` mirror may still hold pre-migration keys or a mix
    of keys and OSM ids, so the authoritative assignment set and its display
    names come from ``user_area_assignments`` + ``areas_osm``.
    """
    require_role(user,'reviewer','responder','publisher','admin')
    if not spatial.postgis_active(db):
        return {'items': []}
    rows=db.execute(text(
        'SELECT ua.area_osm_id, a.name, a.display_name, a.area_type, '
        '(SELECT count(*) FROM report_areas ra JOIN reports r ON r.id = ra.report_id '
        " WHERE ra.area_osm_id = ua.area_osm_id "
        " AND r.state IN ('submitted','under_review','needs_evidence')) AS open_cases, "
        '(SELECT count(*) FROM report_areas ra JOIN reports r ON r.id = ra.report_id '
        " WHERE ra.area_osm_id = ua.area_osm_id AND r.state <> 'draft') AS total_cases "
        'FROM user_area_assignments ua JOIN areas_osm a ON a.id = ua.area_osm_id '
        'WHERE ua.user_id = :uid AND ua.revoked_at IS NULL AND a.active '
        'ORDER BY a.name IS NULL, a.name'), {'uid': user.id}).mappings().all()
    return {'items':[{
        'area_osm_id': r['area_osm_id'],
        'name': r['name'] or r['display_name'] or f"OSM area {r['area_osm_id']}",
        'area_type': r['area_type'],
        'open_cases': r['open_cases'],
        'total_cases': r['total_cases']} for r in rows]}


@router.get('/admin/areas-osm/coverage')
def area_coverage(user:User=Depends(current_user),db:Session=Depends(get_db)):
    """Per-OSM-area load and staffing for access administrators.

    Deliberately uncached: after assigning or deactivating an area the next read
    must reflect it. Counts come from ``report_areas`` (the containment cache)
    so an area's open load is real, not derived from a legacy centroid.
    """
    require_role(user,'admin')
    if not spatial.postgis_active(db):
        return {'items': [], 'total': 0}
    rows=db.execute(text(
        'WITH area_stats AS ('
        '  SELECT ra.area_osm_id,'
        "    count(*) FILTER (WHERE r.state <> 'draft') AS total_cases,"
        "    count(*) FILTER (WHERE r.state IN "
        "      ('submitted','under_review','needs_evidence')) AS open_cases "
        '  FROM report_areas ra JOIN reports r ON r.id = ra.report_id '
        '  GROUP BY ra.area_osm_id),'
        'assignments_count AS ('
        '  SELECT ua.area_osm_id, count(DISTINCT ua.user_id) AS assigned_staff '
        '  FROM user_area_assignments ua JOIN users u ON u.id = ua.user_id '
        "  WHERE ua.revoked_at IS NULL AND u.active "
        "  AND u.roles::jsonb ?| ARRAY['reviewer','responder','publisher','admin'] "
        '  GROUP BY ua.area_osm_id)'
        'SELECT a.id, a.name, a.display_name, a.area_type, a.active,'
        '  COALESCE(st.total_cases,0) AS total_cases,'
        '  COALESCE(st.open_cases,0) AS open_cases,'
        '  COALESCE(ac.assigned_staff,0) AS assigned_staff,'
        '  CASE WHEN COALESCE(st.total_cases,0) > 0 AND COALESCE(ac.assigned_staff,0) = 0 '
        '       THEN true ELSE false END AS needs_staff '
        'FROM areas_osm a '
        'LEFT JOIN area_stats st ON st.area_osm_id = a.id '
        'LEFT JOIN assignments_count ac ON ac.area_osm_id = a.id '
        'WHERE a.active OR COALESCE(st.total_cases,0) > 0 '
        'ORDER BY st.open_cases DESC NULLS LAST, a.name IS NULL, a.name'
    )).mappings().all()
    items=[{
        'id': r['id'],
        'name': r['name'] or r['display_name'] or f'OSM area {r["id"]}',
        'area_type': r['area_type'],
        'active': r['active'],
        'total_cases': r['total_cases'],
        'open_cases': r['open_cases'],
        'assigned_staff': r['assigned_staff'],
        'needs_staff': r['needs_staff']} for r in rows]
    return {'items': items, 'total': len(items)}


@router.get('/admin/audit')
def audit_log(user:User=Depends(current_user),db:Session=Depends(get_db)):
    require_role(user,'admin')
    return {'items':[{'id':a.id,'actor_id':a.actor_id,'action':a.action,'target_id':a.target_id,'created_at':a.created_at}
        for a in db.query(Audit).order_by(Audit.created_at.desc()).limit(300).all()]}

@router.get('/admin/jobs')
def jobs(user:User=Depends(current_user),db:Session=Depends(get_db)):
    require_role(user,'admin')
    return {'items':[{'id':j.id,'kind':j.kind,'state':j.state,'attempts':j.attempts,'last_error':j.last_error,'created_at':j.created_at}
        for j in db.query(Outbox).order_by(Outbox.created_at.desc()).limit(200).all()]}

# Operator-only view of why image assistance is ready, degraded or switched off.
# The public /config response deliberately omits the reason.
@router.get('/admin/image-assistance')
def image_assistance_status(user:User=Depends(current_user)):
    require_role(user,'admin')
    state=ai.status()
    return {'state':state['state'],'model_version':state['model_version'],'detail':state['detail'],
        'provenance':{'package':'speciesnet','version':ai.SPECIESNET_PACKAGE_VERSION,
            'licence':ai.SPECIESNET_LICENCE,'source':ai.SPECIESNET_SOURCE}}

@router.get('/stakeholders')
def stakeholders():
    return {'items':[
        {'name':'Farmers and nearby residents','role':'Report sightings, receive reviewed advisories','workspace':'Community mobile PWA'},
        {'name':'Wetland-adjacent communities','role':'Report observed changes and follow up','workspace':'Wetland reporting'},
        {'name':'Flood-risk communities','role':'Report water observations and read sourced updates','workspace':'Flood information'},
        {'name':'Community liaison users','role':'Support reporting and accessibility','workspace':'Assisted community reporting'},
        {'name':'UWA / conservation reviewers','role':'Proposed wildlife verification and follow-up','workspace':'Review and cases'},
        {'name':'Environment officers / NGOs','role':'Proposed wetland verification and response coordination','workspace':'Wetlands and cases'},
        {'name':'Disaster-management partners','role':'Proposed flood-source review and publication','workspace':'Flood advisories'},
        {'name':'Student team and academic supervisor','role':'Design, evaluate and oversee coursework','workspace':'Research documentation'},
        {'name':'Administrators, providers and funders','role':'Operate services and support delivery','workspace':'Access, audit and integrations'}],
        'note':'Institutional names describe potential stakeholders, not confirmed partnerships.'}
