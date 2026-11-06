import csv, io, hashlib, json
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, Query, Response, BackgroundTasks
from sqlalchemy import or_, and_
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from .db import get_db
from .models import User, Area, Report, Evidence, Review, CaseEvent, Message, Advisory, Prediction, now
from .schemas import ReportWrite, ReportUpdate, VersionRequest, ReviewWrite, AssignWrite, CloseWrite, MessageWrite
from .security import current_user, report_access, is_case_staff, require_role, check_version, audit
from . import spatial
from .cache import touch

router=APIRouter(prefix='/reports',tags=['Reports and cases'])

def _dispatch_pending_safe():
    try:
        from .jobs import dispatch_pending
        dispatch_pending()
    except Exception:
        pass


def visible_query(db, user):
    q = db.query(Report)
    if is_case_staff(user):
        if spatial.postgis_active(db):
            # Staff see every non-draft report; in-area rows render full and
            # out-of-area rows render redacted via spatial.visibility_map.
            return q.filter(or_(Report.owner_id==user.id,Report.state!='draft'))
        return q.filter(or_(Report.owner_id==user.id,and_(Report.area_id.in_(user.areas),Report.state!='draft')))
    return q.filter(Report.owner_id==user.id)

def serialize_report(db,r,user,mode,detail=False):
    if mode=='redacted':
        area=db.get(Area,r.area_id)
        return spatial.redacted_view(r,area.name if area else r.area_id)
    return report_view(db,r,user,detail)

def report_view(db,r,user,detail=False):
    data={k:getattr(r,k) for k in ['id','code','client_id','category','title','description','species','area_id',
        'observed_at','share_location','consent','state','version','assignee_id','created_at','updated_at','ai_priority']}
    data['is_owner']=r.owner_id==user.id
    area=db.get(Area,r.area_id)
    data['area_name']=area.name if area else r.area_id
    data['latitude']=r.latitude
    data['longitude']=r.longitude
    data['evidence']=[{'id':e.id,'filename':e.filename,'width':e.width,'height':e.height,
        'url':f'/api/v1/evidence/{e.id}/content'} for e in db.query(Evidence).filter_by(report_id=r.id).all()]
    if detail:
        data['timeline']=[{'id':e.id,'action':e.action,'note':e.note,'created_at':e.created_at}
            for e in db.query(CaseEvent).filter_by(report_id=r.id).order_by(CaseEvent.created_at).all()]
        data['reviews']=[{'id':e.id,'decision':e.decision,'notes':e.notes,'species':e.species,'created_at':e.created_at}
            for e in db.query(Review).filter_by(report_id=r.id).order_by(Review.created_at).all()]
    return data

def event(db,r,user,action,note=''):
    db.add(CaseEvent(report_id=r.id,actor_id=user.id,action=action,note=note))
    r.updated_at=now()
    audit(db,user,'report.'+action,r.id)

def check_evidence(db,user,ids,report_id=None):
    items=[]
    for evidence_id in set(ids):
        e=db.get(Evidence,evidence_id)
        if not e or e.owner_id!=user.id or (e.report_id and e.report_id!=report_id):
            raise HTTPException(422,'An evidence attachment is unavailable.')
        items.append(e)
    return items

def payload_dict(payload):
    data=payload.model_dump(exclude={'evidence_ids','version'})
    data['observed_at']=payload.observed_at.isoformat()
    return data

@router.get('')
def list_reports(user:User=Depends(current_user),db:Session=Depends(get_db),
    category:str|None=None,state:str|None=None,area_id:str|None=None,q:str=Query('',max_length=160),
    mine:bool=False,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    query=visible_query(db,user)
    if mine:query=query.filter(Report.owner_id==user.id)
    if category:query=query.filter(Report.category==category)
    if state:query=query.filter(Report.state==state)
    if area_id:query=query.filter(Report.area_id==area_id)
    if q:query=query.filter(or_(Report.title.ilike('%'+q+'%'),Report.code.ilike('%'+q+'%')))
    total=query.count()
    from sqlalchemy import case
    priority_order = case((Report.ai_priority=='high',0),(Report.ai_priority=='standard',1),(Report.ai_priority=='low',2),else_=3)
    rows=query.order_by(priority_order, Report.created_at.asc()).offset((page-1)*page_size).limit(page_size).all()
    modes=spatial.visibility_map(db,user,rows)
    return {'items':[serialize_report(db,r,user,modes.get(r.id,'full')) for r in rows],
        'total':total,'page':page,'page_size':page_size}

@router.get('/export.csv')
def export(user:User=Depends(current_user),db:Session=Depends(get_db)):
    require_role(user,'reviewer','publisher','responder')
    output=io.StringIO(); writer=csv.writer(output)
    writer.writerow(['code','category','title','area','status','created_at','updated_at'])
    def safe(v):
        s=str(v or '')
        return "'"+s if s.lstrip().startswith(('=','+','-','@','\t','\r')) else s
    rows=visible_query(db,user).limit(10000).all()
    modes=spatial.visibility_map(db,user,rows)
    for r in rows:
        if modes.get(r.id,'full')=='redacted':
            continue  # export is scoped to acted-on areas only
        writer.writerow([safe(x) for x in [r.code,r.category,r.title,r.area_id,r.state,r.created_at,r.updated_at]])
    audit(db,user,'reports.exported'); db.commit()
    return Response(output.getvalue(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename="ecoguard-reports.csv"'})

@router.post('',status_code=201)
def create(payload:ReportWrite,user:User=Depends(current_user),db:Session=Depends(get_db)):
    if not db.get(Area,payload.area_id):raise HTTPException(422,'Select an existing community.')
    digest=hashlib.sha256(json.dumps(payload.model_dump(mode='json'),sort_keys=True).encode()).hexdigest()
    existing=db.query(Report).filter_by(owner_id=user.id,client_id=payload.client_id).first()
    if existing:
        if existing.request_hash!=digest:raise HTTPException(409,'This draft key has already been used for different content.')
        return report_view(db,existing,user,True)
    attachments=check_evidence(db,user,payload.evidence_ids)
    loc=spatial.resolve_location(db.get(Area,payload.area_id),payload.latitude,payload.longitude,payload.share_location)
    r=Report(**payload_dict(payload),owner_id=user.id,code='EG-'+payload.category[0].upper()+'-'+uuid4().hex[:8].upper(),request_hash=digest)
    if spatial.postgis_active(db) and loc:
        r.latitude=loc['public_lat'];r.longitude=loc['public_lon']
    db.add(r)
    try:db.flush()
    except IntegrityError:
        db.rollback();existing=db.query(Report).filter_by(owner_id=user.id,client_id=payload.client_id).first()
        if existing and existing.request_hash==digest:return report_view(db,existing,user,True)
        raise HTTPException(409,'Draft key conflict. Reload and try again.')
    for e in attachments:e.report_id=r.id
    if spatial.postgis_active(db) and loc:
        spatial.store_report_location(db,r.id,loc['precise_lon'],loc['precise_lat'],loc['public_lon'],loc['public_lat'],loc['precision'],loc['source'])
        spatial.rebuild_report_areas(db,r.id)
    event(db,r,user,'draft','Report saved privately.');db.commit()
    touch('reports.updated',r.id)
    return report_view(db,r,user,True)

@router.get('/{report_id}/predictions')
def report_predictions(report_id:str,user:User=Depends(current_user),db:Session=Depends(get_db)):
    require_role(user,'reviewer','publisher','responder','admin')
    r,_=report_access(db,user,report_id,True)
    evidences=db.query(Evidence).filter_by(report_id=r.id).all()
    items=[]
    best=None
    for e in evidences:
        p=db.query(Prediction).filter_by(evidence_id=e.id).order_by(Prediction.created_at.desc()).first()
        if p is None:
            items.append({'evidence_id':e.id,'state':None,'species':None,'confidence':None,
                'priority':None,'suggested_decision':None,'model_version':None,'created_at':None})
            continue
        items.append({'evidence_id':e.id,'state':p.state,'species':p.species,'confidence':p.confidence,
            'priority':p.priority,'suggested_decision':p.suggested_decision,'model_version':p.model_version,
            'created_at':p.created_at})
        if p.state=='completed' and (best is None or (p.confidence or 0)>(best.confidence or 0)):
            best=p
    priority=None
    if best is not None:
        from .ai_rules import report_priority
        priority=report_priority([x for x in [best]])
        # compute highest across all completed
        completed=[db.query(Prediction).filter_by(evidence_id=e.id).order_by(Prediction.created_at.desc()).first()
                   for e in evidences]
        completed=[p for p in completed if p is not None and p.state=='completed']
        priority=__import__('app.ai_rules',fromlist=['report_priority']).report_priority(completed)
    summary={'best_species':best.species if best else None,'best_confidence':best.confidence if best else None,
        'best_prediction_id':best.id if best else None,'priority':priority,
        'species_mismatch':bool(best and best.species and r.species and r.species!='Unknown animal' and best.species!=r.species)}
    return {'items': items, 'summary': summary}


@router.get('/{report_id}')
def get_report(report_id: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    report, mode = report_access(db, user, report_id, redacted=True)
    return serialize_report(db, report, user, mode, detail=True)

@router.put('/{report_id}')
def update(report_id:str,payload:ReportUpdate,user:User=Depends(current_user),db:Session=Depends(get_db)):
    r,_=report_access(db,user,report_id,True);check_version(r,payload.version)
    if r.owner_id!=user.id or r.state not in ('draft','needs_evidence'):
        raise HTTPException(409,'Only the reporter may edit a draft or a report needing evidence.')
    area=db.get(Area,payload.area_id)
    if not area:raise HTTPException(422,'Unknown community.')
    attachments=check_evidence(db,user,payload.evidence_ids,r.id)
    for key,value in payload_dict(payload).items():
        if key!='client_id':setattr(r,key,value)
    loc=spatial.resolve_location(area,payload.latitude,payload.longitude,payload.share_location)
    if spatial.postgis_active(db) and loc:
        r.latitude=loc['public_lat'];r.longitude=loc['public_lon']
    for e in db.query(Evidence).filter_by(report_id=r.id).all():
        if e.id not in payload.evidence_ids:e.report_id=None
    for e in attachments:e.report_id=r.id
    if spatial.postgis_active(db) and loc:
        spatial.store_report_location(db,r.id,loc['precise_lon'],loc['precise_lat'],loc['public_lon'],loc['public_lat'],loc['precision'],loc['source'])
        spatial.rebuild_report_areas(db,r.id)
    event(db,r,user,'edited','Reporter updated the evidence.');db.commit()
    touch('reports.updated',r.id)
    return report_view(db,r,user,True)

@router.post('/{report_id}/submit')
def submit(report_id:str,payload:VersionRequest,user:User=Depends(current_user),db:Session=Depends(get_db),background_tasks: BackgroundTasks=None):
    r,_=report_access(db,user,report_id,True)
    if r.owner_id!=user.id:raise HTTPException(403,'Only the reporter can submit this report.')
    # Safe retry after the first successful submission.
    if r.state=='submitted':return report_view(db,r,user,True)
    check_version(r,payload.version)
    if r.state not in ('draft','needs_evidence'):raise HTTPException(409,'This report cannot be submitted in its current state.')
    r.state='submitted';event(db,r,user,'submitted','Queued for human review; not a public advisory.')
    # Enqueue image assistance for every attached evidence image. Never blocks submit.
    try:
        from .evidence import enqueue_prediction
        for e in db.query(__import__('app.models', fromlist=['Evidence']).Evidence).filter_by(report_id=r.id).all():
            try:
                enqueue_prediction(db, e, user)
            except Exception as exc:
                import logging
                logging.getLogger(__name__).warning('enqueue prediction failed: %s', exc)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning('prediction enqueue unavailable: %s', exc)
    db.commit()
    if background_tasks is not None:
        background_tasks.add_task(_dispatch_pending_safe)
    touch('reports.updated', r.id)
    return report_view(db, r, user, True)

@router.post('/{report_id}/review')
def review(report_id:str,payload:ReviewWrite,user:User=Depends(current_user),db:Session=Depends(get_db)):
    require_role(user,'reviewer');r,_=report_access(db,user,report_id,True);check_version(r,payload.version)
    if r.owner_id==user.id:raise HTTPException(403,'A different reviewer must assess your own report.')
    if r.state not in ('submitted','under_review','needs_evidence'):
        raise HTTPException(409,'Only an open review can be decided.')
    if r.assignee_id and r.assignee_id!=user.id:raise HTTPException(403,'This case is assigned to a different reviewer.')
    r.state=payload.decision
    if payload.species is not None:r.species=payload.species
    # Save AI snapshot from the best completed prediction, if any.
    ai_pred_id=None;ai_species=None;ai_conf=None
    try:
        preds=db.query(Prediction).join(Evidence, Prediction.evidence_id==Evidence.id).filter(Evidence.report_id==r.id, Prediction.state=='completed').order_by(Prediction.confidence.desc()).first()
        if preds:
            ai_pred_id=preds.id;ai_species=preds.species;ai_conf=preds.confidence
    except Exception:
        pass
    db.add(Review(report_id=r.id,reviewer_id=user.id,decision=payload.decision,notes=payload.notes,species=payload.species,
        ai_prediction_id=ai_pred_id,ai_species=ai_species,ai_confidence=ai_conf))
    event(db,r,user,payload.decision,payload.notes);db.commit()
    touch('reports.updated',r.id)
    return report_view(db,r,user,True)

@router.post('/{report_id}/assign')
def assign(report_id:str,payload:AssignWrite,user:User=Depends(current_user),db:Session=Depends(get_db)):
    require_role(user,'reviewer','responder');r,_=report_access(db,user,report_id,True);check_version(r,payload.version)
    assignee=db.get(User,payload.assignee_id)
    if not assignee or not assignee.active or not set(assignee.roles).intersection({'reviewer','responder'}):
        raise HTTPException(422,'Choose an active reviewer or responder assigned to this area.')
    if spatial.postgis_active(db):
        if not spatial.overlaps_report(db,assignee.id,r.id):
            raise HTTPException(422,'Choose an active reviewer or responder assigned to this area.')
    elif r.area_id not in (assignee.areas or []):
        raise HTTPException(422,'Choose an active reviewer or responder assigned to this area.')
    r.assignee_id=assignee.id;event(db,r,user,'assigned','Case assigned for follow-up.');db.commit()
    touch('reports.updated',r.id)
    return report_view(db,r,user,True)

@router.post('/{report_id}/close')
def close(report_id:str,payload:CloseWrite,user:User=Depends(current_user),db:Session=Depends(get_db)):
    require_role(user,'responder');r,_=report_access(db,user,report_id,True);check_version(r,payload.version)
    if r.state not in ('verified','rejected'):raise HTTPException(409,'Verify or reject the report before closing it.')
    r.state='closed';event(db,r,user,'closed',payload.note);db.commit()
    touch('reports.updated',r.id)
    return report_view(db,r,user,True)

@router.get('/{report_id}/messages')
def messages(report_id:str,user:User=Depends(current_user),db:Session=Depends(get_db)):
    report_access(db,user,report_id)  # full access: redacted-only staff get 403, never messages
    return {'items':[{'id':m.id,'body':m.body,'is_mine':m.sender_id==user.id,'sender_label':'You' if m.sender_id==user.id else 'Case participant',
        'created_at':m.created_at} for m in db.query(Message).filter_by(report_id=report_id).order_by(Message.created_at).all()]}

@router.post('/{report_id}/messages',status_code=201)
def send_message(report_id:str,payload:MessageWrite,user:User=Depends(current_user),db:Session=Depends(get_db)):
    r,_=report_access(db,user,report_id)
    if r.state=='draft':raise HTTPException(409,'Submit the report before sending a message.')
    msg=Message(report_id=report_id,sender_id=user.id,body=payload.body);db.add(msg);audit(db,user,'message.created',report_id);db.commit()
    touch('reports.updated',report_id)
    return {'id':msg.id,'body':msg.body,'is_mine':True,'sender_label':'You','created_at':msg.created_at}
