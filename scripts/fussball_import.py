#!/usr/bin/env python3
"""Import publicly displayed team match plans; fail closed if page layout changes."""
import os, re, sys, hashlib, logging
from datetime import datetime, date
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

def parse(html,team,team_id):
 soup=BeautifulSoup(html,'html.parser')
 table=soup.select_one('.club-matchplan-table, #id-team-matchplan-table, .matchplan-table')
 if not table: raise ValueError('Spielplan-Tabelle nicht gefunden; HTML-Struktur oder Endpoint prüfen')
 rows=table.select('tr')
 found=[]; context=''; seen=set()
 for tr in rows:
  classes=' '.join(tr.get('class',[]))
  if 'row-competition' in classes:
   context=text(tr)
   continue
  clubs=[text(x) for x in tr.select('.club-name') if text(x)]
  if len(clubs)<2:continue
  heim,gast=clubs[:2]
  if not any(re.search(r'FSV\s+Nieder\s*[-–]?\s*Olm',x,re.I) for x in (heim,gast)):
   continue
  dateval=parse_date(context+' '+text(tr))
  if not dateval:continue
  match_link=tr.select_one('a[href*="/spiel/"]') or tr.select_one('a[href*="spiel-id/"]')
  href=match_link.get('href','') if match_link else ''
  # Match identifiers from explicit game links are stable; fallback hash uses fixture date and teams.
  match_id=re.search(r'(?:spiel-id|match-id)/([A-Za-z0-9]+)',href)
  uid=match_id[1] if match_id else hashlib.sha256((dateval+'|'+heim+'|'+gast+'|'+team).encode()).hexdigest()[:32]
  if uid in seen:continue
  seen.add(uid)
  raw=text(tr)
  status=''
  for word in ('abgesetzt','abgesagt','verlegt','ausgefallen','beendet','gespielt'):
   if word in raw.lower():status=word;break
  score_el=tr.select_one('.column-score, .score, .result, .match-result, .column-result')
  score_txt=text(score_el)
  if not score_txt:
   # A score is only accepted from a dedicated score element to avoid parsing times.
   goals=(None,None)
  else:
   goals=parse_score(score_txt)
  if any(word in status for word in ('abgesetzt','abgesagt','verlegt','ausgefallen')):goals=(None,None)
  venue_el=tr.select_one('.column-venue, .venue, .match-venue')
  event={
   'team':team,'quelle_id':uid,
   'quelle_url':(BASE+href if href.startswith('/') else href) if href else f'{BASE}/ajax.team.matchplan/-/team-id/{team_id}',
   'datum':dateval,'anstoss':parse_time(context+' '+raw),
   'heim':heim,'gast':gast,'heim_tore':goals[0],'gast_tore':goals[1],
   'wettbewerb':context[:250] or None,'spielstaette':text(venue_el)[:250] or None,
   'status':status or ('beendet' if goals[0] is not None else 'angesetzt'),
   'aktualisiert_am':datetime.now().astimezone().isoformat()
  }
  found.append(event)
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
   r=sess.get(endpoint,timeout=35);r.raise_for_status()
   games=parse(r.text,team,tid)
   if not games:raise ValueError('Keine Spiele erkannt; Import für dieses Team aus Sicherheitsgründen abgebrochen')
   for offset in range(0,len(games),100):
    batch=games[offset:offset+100]
    response=sess.post(url+'/rest/v1/team_spiele?on_conflict=team,quelle_id',json=batch,headers={
     'apikey':key,'Authorization':'Bearer '+key,'Content-Type':'application/json',
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
