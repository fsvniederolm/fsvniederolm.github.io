#!/usr/bin/env python3
"""Read-only FUSSBALL.DE result diagnostics; no Supabase access."""
import json, pathlib, requests
from bs4 import BeautifulSoup
BASE='https://www.fussball.de'
TID='011MIDBQFK000000VTVG0001VTR8C1K7'
OUT=pathlib.Path('ergebnis-diagnose');OUT.mkdir(exist_ok=True)
URLS={
 'e2_letzte_spiele':f'{BASE}/ajax.team.lastmatches/-/team-id/{TID}',
 'e2_letzte_spiele_html':f'{BASE}/ajax.team.lastmatches/-/mime-type/HTML/team-id/{TID}',
 'e2_spiel_19_okt':f'{BASE}/spiel/sc-moguntia-basara-mainz-jsg-ii-fsv-nieder-olm-ii/-/spiel/031J98VTVC000000VS5489BTVSRD0UH9',
 'e2_spielplan':f'{BASE}/ajax.team.matchplan/-/mime-type/HTML/show-venues/true/team-id/{TID}',
}
s=requests.Session();s.headers.update({'User-Agent':'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/125.0 Safari/537.36','Accept':'text/html,application/xhtml+xml'})
summary=[]
for name,url in URLS.items():
 try:
  r=s.get(url,timeout=35)
  (OUT/(name+'.html')).write_text(r.text,encoding='utf-8')
  soup=BeautifulSoup(r.text,'html.parser')
  info={'name':name,'http':r.status_code,'url':r.url,'bytes':len(r.content),'title':soup.title.get_text(' ',strip=True) if soup.title else None,'score_elements':len(soup.select('.score-left, .score-right')),'club_names':len(soup.select('.club-name'))}
  print(info,flush=True);summary.append(info)
 except Exception as e:
  print(name,repr(e),flush=True);summary.append({'name':name,'error':str(e)})
(OUT/'uebersicht.json').write_text(json.dumps(summary,indent=2,ensure_ascii=False),encoding='utf-8')
