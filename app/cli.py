import argparse,os,secrets
from datetime import datetime,timezone,timedelta
from .db import Base,engine,SessionLocal
from .models import User,Area,Report,Review,CaseEvent,Advisory,LoginSession,now
from .security import hash_password
from .config import get_settings

ROLES={'reporter','reviewer','publisher','responder','admin'}

def _split(values):
    """Accept 'a,b' or ['a','b'] from argparse repeat or comma form."""
    if not values:return []
    out=[]
    for chunk in values:
        out+= [v.strip() for v in str(chunk).split(',') if v.strip()]
    return out

def ensure_admin(email,name,password,roles,areas):
    """Idempotent administrator provisioning.

    Safe to re-run: an existing account is updated in place instead of being rejected,
    so a lost administrator password can be recovered from the server console without
    hand-editing the database. Session revocation on role change matches the API's
    PATCH /admin/users behaviour, so a re-provisioned account cannot keep an old session
    carrying superseded roles.

    roles and areas are None when the flag was omitted, which means "leave them as they
    are" on an existing account, and "admin with no areas" on a new one.
    """
    if get_settings().app_env=='production':
        raise SystemExit('ensure-admin is a local recovery tool. Provision production administrators through the deployment process.')
    if not email:raise SystemExit('--email is required.')
    if len(password or '')<12:raise SystemExit('--password must be at least 12 characters.')
    roles=_split(roles) if roles is not None else None
    if roles is not None:
        roles=sorted(set(roles))
        unknown_roles=sorted(set(roles)-ROLES)
        if unknown_roles:raise SystemExit(f'Unknown role(s): {", ".join(unknown_roles)}. Valid roles: {", ".join(sorted(ROLES))}.')
    # Deduped and sorted so a repeated key cannot land twice in the stored JSON list.
    areas=sorted(set(_split(areas))) if areas is not None else None
    with SessionLocal() as db:
        known={a.id for a in db.query(Area).all()}
        if areas is not None:
            unknown=[a for a in areas if a not in known]
            if unknown:raise SystemExit(f'Unknown area key(s): {", ".join(unknown)}. Create the area first, or omit --areas.')
        address=email.lower()
        u=db.query(User).filter_by(email=address).first()
        if u is None:
            u=User(email=address,name=name or 'Administrator',password_hash=hash_password(password),
                roles=roles or ['admin'],areas=areas or [],preferences={})
            db.add(u);action='created'
        else:
            # Omitted flags are left alone on purpose. An administrator resetting a lost
            # password with a bare re-run must not silently lose the roles and areas that
            # were granted earlier. Pass --roles / --areas to change them, or
            # --areas '' to clear the assignment deliberately.
            u.name=name or u.name;u.password_hash=hash_password(password)
            if roles is not None:u.roles=roles
            if areas is not None:u.areas=areas
            u.active=True;action='updated'
        db.flush()
        db.query(LoginSession).filter_by(user_id=u.id).delete(synchronize_session=False)
        db.commit()
        print(f'Administrator {action}: {address}')
        print(f'  roles: {", ".join(u.roles)}')
        print(f'  areas: {", ".join(u.areas) if u.areas else "none assigned (staff views will be empty)"}')
    if action=='updated':print('  password reset and all existing sessions revoked; sign in again.')

def seed_demo(password):
    if get_settings().app_env=='production' or not get_settings().demo_enabled:
        raise SystemExit('DEMO_ENABLED=true in a non-production environment is required.')
    if len(password)<12:raise SystemExit('Use a demo password of at least 12 characters.')
    with SessionLocal() as db:
        area_data=[('community-a','Community A','Fictional pilot community near western Uganda.',.2,30.1),
            ('community-b','Community B','Fictional river-side pilot area.',.4,30.3),
            ('wetland-a','Wetland zone A','Fictional wetland demonstration area.',.3,32.5)]
        for id,name,desc,lat,lon in area_data:
            if not db.get(Area,id):db.add(Area(id=id,name=name,description=desc,latitude=lat,longitude=lon,radius_km=10))
        db.flush()
        specs=[('reporter','Community reporter',['reporter'],[]),('reviewer','Reviewer A',['reviewer','responder'],[a[0] for a in area_data]),
            ('publisher','Publisher A',['publisher'],[a[0] for a in area_data]),('admin','Access administrator',['admin'],[])]
        users={}
        for key,name,roles,areas in specs:
            email=key+'@ecoguard.example.org';user=db.query(User).filter_by(email=email).first()
            if not user:
                user=User(email=email,name=name,roles=roles,areas=areas,password_hash=hash_password(password),
                    preferences={'followed_areas':[a[0] for a in area_data],'in_app':True,'sms_opt_in':False,'language':'en'})
                db.add(user);db.flush()
            users[key]=user
        if not db.query(Report).first():
            data=[('wildlife','Possible elephant near a field boundary','community-a','under_review','Possible elephant'),
                ('wetland','Possible encroachment at wetland edge','wetland-a','submitted',''),
                ('flood','Rising-water community observation','community-b','verified',''),
                ('wildlife','Unknown animal seen from a distance','community-a','needs_evidence','Unknown animal'),
                ('wildlife','Wildlife sighting reviewed by officer','community-a','verified','Elephant'),
                ('wetland','Follow-up on dumping observation','wetland-a','closed','')]
            for i,(cat,title,area,state,species) in enumerate(data):
                t=(datetime.now(timezone.utc)-timedelta(days=i)).isoformat()
                r=Report(client_id=f'demo-example-{i}',code=f'EG-{cat[0].upper()}-{24-i:03d}',owner_id=users['reporter'].id,
                    area_id=area,category=cat,title=title,description='Illustrative coursework record, not an actual incident. Observation requires human verification.',
                    species=species,observed_at=t,consent=True,share_location=False,state=state,created_at=t,updated_at=t)
                db.add(r);db.flush()
                db.add(CaseEvent(report_id=r.id,actor_id=users['reporter'].id,action='submitted',note='Illustrative seeded record.',created_at=t))
                if state in ('verified','closed'):
                    db.add(Review(report_id=r.id,reviewer_id=users['reviewer'].id,decision='verified',notes='Demo review only, not a real-world finding.',created_at=t))
                if state=='verified':
                    db.add(Advisory(report_id=r.id,area_id=r.area_id,author_id=users['publisher'].id,publisher_id=users['publisher'].id,
                        category=cat,title=('Wildlife advisory' if cat=='wildlife' else 'Flood information')+' — example',
                        body='Coursework example: stay at a safe distance, do not approach wildlife or enter floodwater, and follow guidance from the relevant responders.',
                        source='Illustrative coursework review, not an official warning',state='published',published_at=t,
                        expires_at=(datetime.now(timezone.utc)+timedelta(days=7)).isoformat()))
        db.commit()
    print('Demo accounts: reporter@ecoguard.example.org, reviewer@ecoguard.example.org, publisher@ecoguard.example.org, admin@ecoguard.example.org')
    print('Use the password you supplied. Demo data must not be used as real incident evidence.')

def main():
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['init-local','seed-demo','create-admin','ensure-admin'])
    parser.add_argument('--password',default=os.getenv('DEMO_PASSWORD',''));parser.add_argument('--email');parser.add_argument('--name',default='Administrator')
    parser.add_argument('--roles',action='append',default=None,help='Role to grant. Repeatable or comma-separated. Omit to leave unchanged, or admin on a new account.')
    parser.add_argument('--areas',action='append',default=None,help='Area key to assign. Repeatable or comma-separated. Omit to leave unchanged; pass --areas "" to clear.')
    args=parser.parse_args()
    if args.command=='init-local':
        if get_settings().app_env=='production':raise SystemExit('Use Alembic migrations in production.')
        Base.metadata.create_all(engine)
    elif args.command=='seed-demo':seed_demo(args.password)
    elif args.command=='ensure-admin':ensure_admin(args.email,args.name,args.password,args.roles,args.areas)
    else:
        if not args.email or len(args.password)<12:raise SystemExit('--email and a 12+ character --password are required.')
        with SessionLocal() as db:
            if db.query(User).filter_by(email=args.email.lower()).first():raise SystemExit('Account already exists.')
            db.add(User(email=args.email.lower(),name=args.name,password_hash=hash_password(args.password),roles=['admin'],areas=[]));db.commit()
        print('Administrator created; review and publication roles have NOT been granted.')
if __name__=='__main__':main()
