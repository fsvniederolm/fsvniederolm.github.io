#!/usr/bin/env python3
"""Import publicly displayed team match plans; fail closed if page layout changes."""
import os, re, sys, hashlib, logging
from datetime import datetime, date, timedelta
from urllib.parse import quote
import requests
from io import BytesIO
from fontTools.ttLib import TTFont
from bs4 import BeautifulSoup

TEAMS = {
 'E3':'02TG8FBOR4000000VS5489BRVU522QNF',
 'E2':'011MIDBQFK000000VTVG0001VTR8C1K7',
 'E1':'011MIAOESO000000VTVG0001VTR8C1K7',
 'D1':'011MID4NV4000000VTVG0001VTR8C1K7',
 'C2':'031270457K000000VS5489BSVVHO3SPG',
 'C1':'03126V39C8000000VS5489BSVVHO3SPG',
 'Zweite':'011MI9J5UO000000VTVG0001VTR8C1K7',
 'Erste':'011MIAJIK8000000VTVG0001VTR8C1K7',
}
BASE='https://www.fussball.de'
HEADERS={'User-Agent':'Mozilla/5.0 (compatible; FSV-Teamplan/1.0; club fixture sync)', 'Accept':'text/html,application/xhtml+xml'}
log=logging.getLogger('teamplan')

def text(el):
 return el.get_text(' ',strip=True) if el else ''

def parse_date(raw):
 m=re.search(r'(\d{1,2})\.(\d{1,2})\.(\d{4})',raw)
 if not m: return None
 try:return date(int(m[3]),int(m[2]),int(m[1])).isoformat()
 except ValueError:return None

def parse_time(raw):
 m=re.search(r'(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)',raw)
 return f'{int(m[1]):02d}:{m[2]}:00' if m else None

def parse_score(raw):
 # Only explicit final scores; no scores for scheduled / postponed games.
 m=re.search(r'(?<!\d)(\d{1,2})\s*[:\-]\s*(\d{1,2})(?!\d)',raw)
 return (int(m[1]),int(m[2])) if m else (None,None)

DIGITS = {'zero':'0','one':'1','two':'2','three':'3','four':'4','five':'5','six':'6','seven':'7','eight':'8','nine':'9'}
FONT_CACHE = {}

def decode_obfuscated(el, session):
    """Decode FUSSBALL.DE per-page obfuscation via the original font cmap."""
    if el is None:
        return None
    font_id = el.get('data-obfuscation')
    raw = el.get_text('', strip=True)
    if not font_id:
        return raw if raw.isdigit() else None
    if not re.fullmatch(r'[a-z0-9]{5,20}', font_id):
        return None
    if font_id not in FONT_CACHE:
        font_url = f'{BASE}/export.fontface/-/format/ttf/id/{font_id}/type/font'
        response = session.get(font_url, timeout=25)
        response.raise_for_status()
        if len(response.content) > 1_000_000:
            raise ValueError('Unerwartet große Schriftdatei')
        cmap = TTFont(BytesIO(response.content)).getBestCmap()
        FONT_CACHE[font_id] = {code: DIGITS[name] for code, name in cmap.items() if name in DIGITS}
    decoded = ''.join(FONT_CACHE[font_id].get(ord(ch), '?') for ch in raw)
    return decoded if decoded.isdigit() else None

def score_from_element(root, session):
    left = root.select_one('.score-left')
    right = root.select_one('.score-right')
    if left is None or right is None:
        return None
    a = decode_obfuscated(left, session)
    b = decode_obfuscated(right, session)
    if a is None or b is None:
        return None
    return int(a), int(b)

def detail_score(url, session):
    """Only use a real final result from a match detail page."""
    if not url.startswith(BASE + '/spiel/'):
        return None
    response = session.get(url, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    for el in soup.select('.end-result'):
        result = score_from_element(el, session)
        if result is not None:
            return result
    return None

def parse(html, team, team_id, session):
    """FUSSBALL.DE uses separate headline, competition, fixture and venue rows."""
    soup = BeautifulSoup(html, 'html.parser')
    tables = soup.select('table.table-full-width, .club-matchplan-table, #id-team-matchplan-table, .matchplan-table')
    if not tables:
        raise ValueError('Keine Spielplantabelle gefunden')
    found, seen = [], set()
    for table in tables:
        current_date = current_time = current_comp = None
        pending = None
        for tr in table.select('tr'):
            classes = set(tr.get('class', []))
            raw = text(tr)
            if 'row-headline' in classes:
                current_date = parse_date(raw)
                current_time = parse_time(raw)
                if '|' in raw:
                    current_comp = raw.split('|', 1)[1].strip()[:250]
                pending = None
                continue
            if 'row-competition' in classes:
                date_cell = tr.select_one('.column-date')
                # The desktop date is DD.MM.YY, while the mobile headline uses DD.MM.YYYY.
                date_str = text(date_cell)
                if not current_date:
                    short = re.search(r'(\d{1,2})\.(\d{1,2})\.(\d{2})(?!\d)', date_str)
                    if short:
                        current_date = date(2000 + int(short[3]), int(short[2]), int(short[1])).isoformat()
                current_time = parse_time(date_str) or current_time
                comp = tr.select_one('.column-team')
                current_comp = text(comp)[:250] or current_comp
                pending = None
                continue
            if 'row-venue' in classes:
                if pending is not None:
                    cells = tr.select('td')
                    venue = max((text(td) for td in cells), key=len, default='')
                    pending['spielstaette'] = venue[:250] or None
                continue
            clubs = tr.select('td.column-club .club-name')
            if len(clubs) != 2 or not current_date:
                continue
            heim, gast = (text(clubs[0]), text(clubs[1]))
            # Exact team ID is safer than matching club names, especially for E1/E2/E3.
            club_links = tr.select('td.column-club a.club-wrapper[href]')
            if not any('/team-id/' + team_id in a.get('href', '') for a in club_links):
                continue
            game_link = tr.select_one('a[href*="/spiel/"]')
            href = game_link.get('href', '') if game_link else ''
            # FUSSBALL.DE links may be /spiel/<slug>/spiel-id/<ID>/.
            # The slug (e.g. 'fsv') is NOT a unique match identifier.
            match_id = re.search(r'/spiel-id/([A-Za-z0-9]+)(?:/|$)', href)
            # A deterministic globally unique fallback works with BOTH database
            # constraints: UNIQUE(quelle_id) and UNIQUE(team, quelle_id).
            uid = ('fd-' + match_id.group(1)) if match_id else 'fs-' + hashlib.sha256(
                ('|'.join([team, current_date, current_time or '', heim, gast, current_comp or ''])).encode('utf-8')
            ).hexdigest()[:40]
            if uid in seen:
                continue
            seen.add(uid)
            score_cell = tr.select_one('td.column-score')
            # FUSSBALL.DE shows rescheduled matches at the ORIGINAL date too.
            # Such rows contain an info-text with the new date, not a result.
            # Never fetch the detail result and attach it to the old date.
            relocation = text(score_cell.select_one('.info-text')) if score_cell else ''
            relocated_to = parse_date(relocation)
            if relocated_to and relocated_to != current_date:
                log.info('%s: verlegten Alttermin %s -> %s übersprungen: %s gegen %s',
                         team, current_date, relocated_to, heim, gast)
                continue
            score_txt = text(score_cell)
            low = score_txt.lower()
            cancelled = any(x in low for x in ('absetzung', 'abgesetzt', 'abgesagt', 'ausfall', 'verlegt'))
            goals = (None, None) if cancelled else parse_score(score_txt)
            if not cancelled and current_date <= date.today().isoformat():
                # Prefer the page-specific font; avoid accepting dates or other numbers as scores.
                try:
                    decoded = score_from_element(tr.select_one('td.column-score'), session)
                    if decoded is not None:
                        goals = decoded
                except Exception as exc:
                    log.warning('%s: Schrift-Entschlüsselung fehlgeschlagen: %s', team, exc)
                if goals[0] is None and href:
                    try:
                        detail = detail_score(href if href.startswith('http') else BASE + href, session)
                        if detail is not None:
                            goals = detail
                    except Exception as exc:
                        log.warning('%s: Spiel-Detail nicht lesbar (%s): %s', team, href, exc)
            status = ('abgesetzt' if cancelled else
                      'beendet' if goals[0] is not None else 'angesetzt')
            event = {
                'team': team, 'quelle_id': uid,
                'quelle_url': href if href.startswith('http') else BASE + href if href.startswith('/') else f'{BASE}/mannschaft/-/team-id/{team_id}',
                'datum': current_date, 'anstoss': current_time,
                'heim': heim, 'gast': gast,
                'heim_tore': goals[0], 'gast_tore': goals[1],
                'wettbewerb': current_comp, 'spielstaette': None,
                'status': status,
                'aktualisiert_am': datetime.now().astimezone().isoformat()
            }
            found.append(event)
            pending = event
    return found

def cleanup_stale_reschedules(sess, url, key, team, games):
    """Remove obsolete synthetic IDs only when the current source is unambiguous.

    A genuine second match on the same day is never removed if it still appears
    in the current FUSSBALL.DE matchplan. No cleanup on partial/failed imports.
    """
    from collections import defaultdict
    current = defaultdict(list)
    for game in games:
        if game['status'] != 'abgesetzt':
            current[(game['datum'], game['heim'].casefold(), game['gast'].casefold())].append(game)
    # Only compare dates in the imported source. Never delete outside its coverage.
    dates = sorted({g['datum'] for g in games})
    if not dates:
        return 0
    headers = {'apikey': key, 'Authorization': 'Bearer ' + key, 'Accept': 'application/json'}
    resp = sess.get(url + '/rest/v1/team_spiele', params={
        'select': 'team,quelle_id,datum,anstoss,heim,gast,status',
        'team': 'eq.' + team, 'datum': 'gte.' + dates[0],
        'order': 'datum.asc', 'limit': '1000'
    }, headers=headers, timeout=35)
    resp.raise_for_status()
    existing = resp.json()
    if len(existing) >= 1000:
        log.warning('%s: Bereinigung übersprungen (Ergebnislimit erreicht)', team)
        return 0
    current_ids = {g['quelle_id'] for g in games}
    removed = 0
    for row in existing:
        key_tuple = (row['datum'], (row.get('heim') or '').casefold(), (row.get('gast') or '').casefold())
        matches = current.get(key_tuple, [])
        # Exactly one fixture in current source; otherwise possible double-header.
        if len(matches) != 1 or row['quelle_id'] in current_ids:
            continue
        new = matches[0]
        # Only synthetic IDs that encode the old kickoff. Never remove stable match IDs.
        if not str(row['quelle_id']).startswith('fs-') or not str(new['quelle_id']).startswith('fs-'):
            continue
        if not row.get('anstoss') or not new.get('anstoss') or row['anstoss'] == new['anstoss']:
            continue
        # Restrict to upcoming and very recent games; protect historical archives.
        if row['datum'] < (date.today() - timedelta(days=7)).isoformat():
            continue
        delete = sess.delete(url + '/rest/v1/team_spiele', params={
            'team': 'eq.' + team, 'quelle_id': 'eq.' + row['quelle_id'],
            'datum': 'eq.' + row['datum'], 'anstoss': 'eq.' + row['anstoss']
        }, headers={**headers, 'Prefer': 'return=minimal'}, timeout=35)
        delete.raise_for_status()
        removed += 1
        log.info('%s: veralteten Termin entfernt: %s %s -> %s, %s gegen %s',
                 team, row['datum'], row['anstoss'], new['anstoss'], row['heim'], row['gast'])
    return removed

def cleanup_cancelled_games(sess, url, key, team, games):
    """Remove confirmed cancelled fixtures from DB, never store them as upcoming.

    Match either the same source ID or the same teams/date (when unique).
    Avoid deleting another genuine fixture when multiple same-day games exist.
    """
    from collections import Counter
    cancelled = [g for g in games if g['status'] == 'abgesetzt']
    if not cancelled:
        return 0
    headers = {'apikey': key, 'Authorization': 'Bearer ' + key, 'Accept': 'application/json'}
    counts = Counter((g['datum'], g['heim'].casefold(), g['gast'].casefold()) for g in games)
    removed = 0
    for game in cancelled:
        # Do not remove a live replacement on the same date.
        ident = (game['datum'], game['heim'].casefold(), game['gast'].casefold())
        if counts[ident] != 1:
            log.warning('%s: Absetzung mehrdeutig, keine Löschung: %s %s gegen %s', team, *ident)
            continue
        resp = sess.get(url + '/rest/v1/team_spiele', params={
            'select': 'team,quelle_id,datum,anstoss,heim,gast,status',
            'team': 'eq.' + team, 'datum': 'eq.' + game['datum'], 'limit': '200'
        }, headers=headers, timeout=35)
        resp.raise_for_status()
        rows = resp.json()
        if len(rows) >= 200:
            raise RuntimeError('Absetzungsbereinigung abgebrochen: zu viele DB-Zeilen')
        for row in rows:
            if (row['quelle_id'] != game['quelle_id'] and
                (row.get('heim') or '').casefold() != game['heim'].casefold()):
                continue
            if (row['quelle_id'] != game['quelle_id'] and
                (row.get('gast') or '').casefold() != game['gast'].casefold()):
                continue
            resp = sess.delete(url + '/rest/v1/team_spiele', params={
                'team': 'eq.' + team, 'quelle_id': 'eq.' + row['quelle_id'],
                'datum': 'eq.' + row['datum']
            }, headers={**headers, 'Prefer': 'return=minimal'}, timeout=35)
            resp.raise_for_status()
            removed += 1
            log.info('%s: abgesetztes Spiel entfernt: %s %s gegen %s (ID %s)',
                     team, row['datum'], row['heim'], row['gast'], row['quelle_id'])
    return removed

def cleanup_missing_from_source(sess, url, key, team, games):
    """Reconcile upcoming fixtures with the complete current FUSSBALL.DE snapshot.

    Delete only auto-imported rows that are no longer present in the source.
    Fail closed when the feed looks incomplete; never delete historical results.
    """
    active = [g for g in games if g['status'] != 'abgesetzt']
    today = date.today().isoformat()
    future = [g for g in active if g['datum'] >= today]
    if len(games) < 10 or len(future) < 3:
        log.warning('%s: Abgleich übersprungen: Spielplan möglicherweise unvollständig (%s Spiele, %s zukünftig)',
                    team, len(games), len(future))
        return 0
    # Only the future portion actually covered by the source is reconciled.
    end = max(g['datum'] for g in future)
    ids = {g['quelle_id'] for g in active}
    headers = {'apikey': key, 'Authorization': 'Bearer ' + key, 'Accept': 'application/json'}
    resp = sess.get(url + '/rest/v1/team_spiele', params={
        'select': 'team,quelle_id,datum,anstoss,heim,gast,status',
        'team': 'eq.' + team, 'datum': 'gte.' + today, 'datum': 'lte.' + end,
        'order': 'datum.asc', 'limit': '1000'
    }, headers=headers, timeout=35)
    resp.raise_for_status()
    rows = resp.json()
    if len(rows) >= 1000:
        raise RuntimeError(f'{team}: DB-Ergebnislimit erreicht; keine Löschung')
    stale = [r for r in rows if str(r.get('quelle_id', '')).startswith(('fs-', 'fd-'))
             and r['quelle_id'] not in ids]
    # Detect truncated/changed source before deleting large fractions of stored fixtures.
    if len(stale) > 3 and len(stale) > max(1, len(rows) // 4):
        raise RuntimeError(f'{team}: {len(stale)} von {len(rows)} zukünftigen DB-Spielen fehlen in Quelle; '
                           'Sicherheitsstopp, keine Löschung')
    removed = 0
    for row in stale:
        response = sess.delete(url + '/rest/v1/team_spiele', params={
            'team': 'eq.' + team, 'quelle_id': 'eq.' + row['quelle_id'],
            'datum': 'eq.' + row['datum']
        }, headers={**headers, 'Prefer': 'return=minimal'}, timeout=35)
        response.raise_for_status()
        removed += 1
        log.info('%s: nicht mehr auf FUSSBALL.DE vorhandenes Spiel entfernt: %s %s %s gegen %s (%s)',
                 team, row['datum'], row.get('anstoss'), row['heim'], row['gast'], row['quelle_id'])
    return removed

def main():
 logging.basicConfig(level=logging.INFO,format='%(levelname)s %(message)s')
 url=os.environ.get('SUPABASE_URL','').rstrip('/')
 key=os.environ.get('SUPABASE_SERVICE_ROLE_KEY','')
 if not url or not key:raise RuntimeError('SUPABASE_URL und SUPABASE_SERVICE_ROLE_KEY als Secrets setzen')
 sess=requests.Session();sess.headers.update(HEADERS)
 imported=0; failed=[]
 for team,tid in TEAMS.items():
  endpoint=f'{BASE}/ajax.team.matchplan/-/mime-type/HTML/show-venues/true/team-id/{quote(tid)}'
  try:
   today=date.today()
   start=(today-timedelta(days=120)).isoformat()
   end=(today+timedelta(days=180)).isoformat()
   candidates=[
    f'{BASE}/ajax.team.matchplan/-/datum-von/{start}/datum-bis/{end}/max/300/match-type/-1/wettkampftyp/-1/show-venues/true/mime-type/HTML/team-id/{quote(tid)}',
    endpoint,
   ]
   games=[]
   for idx,candidate in enumerate(candidates,1):
    r=sess.get(candidate,timeout=35)
    soup=BeautifulSoup(r.text,'html.parser')
    tables=soup.select('.club-matchplan-table, #id-team-matchplan-table, .matchplan-table')
    club_names=soup.select('.club-name')
    title=text(soup.title)[:100]
    log.info('%s: Abruf %s HTTP=%s Bytes=%s Tabellen=%s Vereinsnamen=%s Titel=%r URL=%s',
             team,idx,r.status_code,len(r.content),len(tables),len(club_names),title,r.url)
    if r.status_code != 200:
     log.warning('%s: Abruf %s lieferte HTTP %s',team,idx,r.status_code)
     continue
    try:
     games=parse(r.text,team,tid,sess)
    except ValueError as exc:
     log.warning('%s: Abruf %s: %s',team,idx,exc)
     continue
    log.info('%s: Abruf %s erkannte %s Spiele',team,idx,len(games))
    if games:break
   if not games:raise ValueError('Keine Spiele erkannt. Bitte HTTP/Tabellen/Vereinsnamen-Diagnose oben prüfen; kein Schreiben durchgeführt')
   active_games=[g for g in games if g['status'] != 'abgesetzt']
   for offset in range(0,len(active_games),100):
    batch=active_games[offset:offset+100]
    response=sess.post(url+'/rest/v1/team_spiele?on_conflict=team,quelle_id',json=batch,headers={
     'apikey':key,'Authorization':'Bearer '+key,'Content-Type':'application/json','Accept':'application/json',
     'Prefer':'resolution=merge-duplicates,return=minimal'
    },timeout=35)
    if not response.ok:raise RuntimeError(f'Supabase {response.status_code}: {response.text[:300]}')
   # Only after a successful upsert; errors here fail the workflow visibly.
   cleanup_stale_reschedules(sess,url,key,team,games)
   cleanup_cancelled_games(sess,url,key,team,games)
   cleanup_missing_from_source(sess,url,key,team,games)
   imported+=len(games)
   log.info('%s: %s aktive Spiele übernommen, %s Absetzungen erkannt',team,len(active_games),len(games)-len(active_games))
  except Exception as exc:
   failed.append(team);log.error('%s: %s',team,exc)
 if failed:raise RuntimeError('Import nicht vollständig. Fehlerhafte Teams: '+', '.join(failed))
 log.info('Fertig: %s Spiele aus %s Teams.',imported,len(TEAMS))

if __name__=='__main__':
 try:main()
 except Exception as exc:logging.error('%s',exc);sys.exit(1)
