#!/usr/bin/env python3
"""Import publicly displayed team match plans; fail closed if page layout changes."""
import os, re, sys, hashlib, logging
from datetime import datetime, date, timedelta
from urllib.parse import quote
import requests
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

def parse(html, team, team_id):
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
            match_id = re.search(r'/spiel/([A-Za-z0-9]+)', href)
            uid = match_id[1] if match_id else hashlib.sha256(
                (current_date + '|' + heim + '|' + gast + '|' + team).encode()
            ).hexdigest()[:32]
            if uid in seen:
                continue
            seen.add(uid)
            score_txt = text(tr.select_one('td.column-score'))
            low = score_txt.lower()
            cancelled = any(x in low for x in ('absetzung', 'abgesetzt', 'abgesagt', 'ausfall', 'verlegt'))
            goals = (None, None) if cancelled else parse_score(score_txt)
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
     games=parse(r.text,team,tid)
    except ValueError as exc:
     log.warning('%s: Abruf %s: %s',team,idx,exc)
     continue
    log.info('%s: Abruf %s erkannte %s Spiele',team,idx,len(games))
    if games:break
   if not games:raise ValueError('Keine Spiele erkannt. Bitte HTTP/Tabellen/Vereinsnamen-Diagnose oben prüfen; kein Schreiben durchgeführt')
   for offset in range(0,len(games),100):
    batch=games[offset:offset+100]
    response=sess.post(url+'/rest/v1/team_spiele?on_conflict=team,quelle_id',json=batch,headers={
     'apikey':key,'Authorization':'Bearer '+key,'Content-Type':'application/json','Accept':'application/json',
     'Prefer':'resolution=merge-duplicates,return=minimal'
    },timeout=35)
    if not response.ok:raise RuntimeError(f'Supabase {response.status_code}: {response.text[:300]}')
   imported+=len(games)
   log.info('%s: %s Spiele übernommen',team,len(games))
  except Exception as exc:
   failed.append(team);log.error('%s: %s',team,exc)
 if failed:raise RuntimeError('Import nicht vollständig. Fehlerhafte Teams: '+', '.join(failed))
 log.info('Fertig: %s Spiele aus %s Teams.',imported,len(TEAMS))

if __name__=='__main__':
 try:main()
 except Exception as exc:logging.error('%s',exc);sys.exit(1)
