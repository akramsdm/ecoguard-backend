"""Seed published demo advisories around the capital region (Entebbe/Kampala).

The geographic-areas rollout demo keeps its published advisories in the western
Uganda pilot areas, so the public "near me" page shows nothing from Kampala and
Entebbe — where the app is actually demonstrated. This script inserts verified,
published demo reports around Entebbe and Kampala, generalised onto the public
~1 km grid, so the near-me page and community map have spots to show there.

Run after ``alembic upgrade head`` (migration 0006 mirrors OSM districts into
``areas``): ``python -m app.seed_demo_advisories``. Idempotent — a fixed
``demo-nearby-*`` client_id prefix means a second run exits without writing.
Inserts are clearly illustrative courseware records, never real incidents.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from app.db import SessionLocal
from app.models import Area, User, Report, Review, CaseEvent, Advisory, uid, now
from app.cache import touch
from app.config import get_settings
from app import spatial

# (key, area_id, category, code, report title, species, lat, lon, advisory title)
DEMO = [
    dict(key='entebbe-elephant', area_id='823', category='wildlife',
         code='EG-DEMO-W-905', title='Elephant close to the Entebbe–Kampala Highway',
         species='African elephant', lat=0.058, lon=32.453,
         adv_title='Wildlife advisory — Entebbe area',
         adv_body='Illustrative demo advisory for the geographic-areas rollout: a '
                  'human-verified wildlife sighting was reported near Entebbe. Keep '
                  'your distance, do not approach the animal, and follow guidance '
                  'from the relevant responders. The exact position is withheld.'),
    dict(key='kampala-flood', area_id='858', category='flood',
         code='EG-DEMO-F-906', title='Rising water levels in the Namanve lowlands',
         species='', lat=0.296, lon=32.587,
         adv_title='Flood information — Kampala lowlands',
         adv_body='Illustrative demo advisory for the geographic-areas rollout: '
                  'standing water has been observed in a low-lying Kampala area. '
                  'Avoid flooded roads, do not drive through water of unknown '
                  'depth, and follow local authority guidance.'),
    dict(key='entebbe-wetland', area_id='857', category='wetland',
         code='EG-DEMO-L-907', title='Suspected wetland clearing on the lake shore',
         species='', lat=0.022, lon=32.441,
         adv_title='Wetland observation — Entebbe shoreline',
         adv_body='Illustrative demo advisory for the geographic-areas rollout: '
                  'a suspected wetland clearance was observed near the lake shore. '
                  'This is an observation, not an established offence; verified '
                  'details are shared only with the responsible case team.'),
]

_DESCRIPTION = ('Illustrative courseware record, not an actual incident. '
                'Observation requires human verification.')
_ADV_SOURCE = 'Illustrative courseware review — not an official warning.'
_ADV_NOTE = 'Demo review only, not a real-world finding.'
_EVENT_NOTE = 'Illustrative seeded record.'


def seed() -> None:
    if get_settings().app_env == 'production':
        raise SystemExit('Refusing to seed demo advisories in production.')
    with SessionLocal() as db:
        if db.query(Report).filter(Report.client_id.like('demo-nearby-%')).first():
            print('Demo advisories already seeded; nothing to do.')
            return
        missing = [r['key'] for r in DEMO
                   if db.get(Area, r['area_id']) is None]
        if missing:
            raise SystemExit('Missing areas (run alembic upgrade head first): '
                             f'{", ".join(missing)}')
        users = db.query(User).all()
        owner = next((u for u in users if 'admin' in u.roles
                      and u.email == 'akram@gmail.com'), None) or \
            next((u for u in users if 'admin' in u.roles), None)
        if owner is None:
            raise SystemExit('No admin account to own the demo evidence.')
        reviewer = next((u for u in users if 'reviewer' in u.roles), None) or owner
        publisher = next((u for u in users if 'publisher' in u.roles), None) or owner

        t = now()
        exp = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        created = []
        for row in DEMO:
            # Grid-generalised public point; the precise fix is kept internal.
            glon, glat = spatial.grid_point(row['lon'], row['lat'])
            report_id = uid()
            r = Report(
                id=report_id, client_id='demo-nearby-' + row['key'], code=row['code'],
                owner_id=owner.id, area_id=row['area_id'], category=row['category'],
                title=row['title'], description=_DESCRIPTION, species=row['species'],
                observed_at=t, consent=True, share_location=True,
                latitude=glat, longitude=glon, state='verified',
                created_at=t, updated_at=t)
            db.add(r)
            # Persist the report row before its dependent rows: the location UPDATE
            # and the advisory insert both reference it directly.
            db.flush()
            db.add(CaseEvent(report_id=report_id, actor_id=owner.id,
                             action='submitted', note=_EVENT_NOTE, created_at=t))
            db.add(Review(report_id=report_id, reviewer_id=reviewer.id,
                          decision='verified', notes=_ADV_NOTE, created_at=t))
            spatial.store_report_location(db, report_id, row['lon'], row['lat'],
                                          glon, glat, 'generalised', 'manual')
            a = Advisory(report_id=report_id, area_id=row['area_id'], author_id=publisher.id,
                         publisher_id=publisher.id, category=row['category'],
                         title=row['adv_title'], body=row['adv_body'],
                         source=_ADV_SOURCE, state='published',
                         expires_at=exp, created_at=t, published_at=t)
            db.add(a)
            created.append((a.id, row['code'], row['category'], row['adv_title']))
        db.commit()
    for advisory_id, code, category, title in created:
        touch('advisory.published', advisory_id)
        print(f'  {code}  {category:8s} {title} ({advisory_id})')
    print(f'Seeded {len(created)} published demo advisories around Entebbe/Kampala.')


if __name__ == '__main__':
    sys.exit(seed())