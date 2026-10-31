#!/usr/bin/env python3
"""FSV: Spielstaetten-Konflikte erkennen und per STRATO SMTP melden."""
import os, json, hashlib, smtplib, ssl, sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from email.message import EmailMessage
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen

BASE = os.environ['SUPABASE_URL'].rstrip('/') + '/rest/v1'
KEY = os.environ['SUPABASE_SERVICE_ROLE_KEY']
HEAD = {'apikey': KEY, 'Authorization': 'Bearer '+KEY, 'Content-Type':'application/json'}
TZ = ZoneInfo('Europe/Berlin')
PITCHES = {'Nieder-Olm Kleinfeld KR':'kleinfeld', 'Nieder-Olm KR':'gross', 'Nieder-Olm RA':'rasen'}

def api(path, method='GET', body=None, extra=None):
    h={**HEAD,**(extra or {})}
    req=Request(BASE+path, data=json.dumps(body).encode() if body is not None else None, headers=h, method=method)
    with urlopen(req, timeout=35) as r:
        raw=r.read()
        return json.loads(raw) if raw else None

def minutes(t):
    h,m=map(int,str(t)[:5].split(':'));return 60*h+m

def fmt(m): return f'{m//60:02d}:{m%60:02d}'

def pitch(s):
    s=str(s or '').lower()
    if 'kleinfeld' in s: return 'kleinfeld'
    if 'kunstrasen an der igs' in s or s=='nieder-olm kr' or s.endswith(' nieder-olm kr'):return 'gross'
    if 'rasen' in s or s=='nieder-olm ra' or s.endswith(' nieder-olm ra'):return 'rasen'
    return None

def cancelled(s):return any(w in str(s or '').lower() for w in ('abgesetzt','abgesagt','ausgefallen','spielausfall','nicht statt','cancel','absetz'))

def is_small_game(r):
    # Nur eindeutig erkennbare E- und F-Jugendspiele brauchen halben grossen KR.
    import re
    return bool(re.search(r'(?<![a-z0-9])(?:e|f)(?:[- ]?jugend|[1-9]\b|\b)',str(r.get('liga') or '').lower()))

def fetch_day(day):
    date=quote(day)
    games=api('/spielstaetten_belegung?'+urlencode({'select':'datum,anstoss,spielstaette,liga,heim,gast,kennung,status','datum':'eq.'+day,'order':'anstoss.asc'}))
    school=api('/fussballschule_belegung?'+urlencode({'select':'id,datum,uhrzeit_von,uhrzeit_bis,spielstaette,titel,notiz','datum':'eq.'+day}))
    training=api('/rpc/public_trainingsbelegung',method='POST',body={'p_datum':day})
    if not isinstance(games,list) or not isinstance(school,list) or not isinstance(training,list):raise RuntimeError('Unvollstaendige Supabase-Antwort')
    events=[]
    for g in games:
        if cancelled(g.get('status')):continue
        p=pitch(g.get('spielstaette'))
        if not p:continue
        s=minutes(g['anstoss']); duration=180 if 'turnier' in ' '.join(str(g.get(k) or '') for k in ('liga','heim','gast')).lower() else 60
        # Wie in spielstaettenbelegung.html: normales Spiel 60 Min, Turnier 180 Min.
        events.append({'type':'Spiel','pitch':p,'s':s,'e':s+duration,'name':f"{g.get('heim') or '?'} – {g.get('gast') or '?'} ({g.get('liga') or 'Spiel'})",'key':str(g.get('kennung') or '')+'|'+str(g.get('heim'))+'|'+str(g.get('gast')),'need':0.5 if p=='gross' and is_small_game(g) else 1})
    for t in training:
        p={'kaefig':'kleinfeld','igs':'gross','rasen':'rasen'}.get(t.get('field'))
        if not p:continue
        s,e=minutes(t['start_time']),minutes(t['end_time'])
        if e>s:events.append({'type':'Training','pitch':p,'s':s,'e':e,'name':str(t.get('team') or 'Training'),'key':str(t.get('team'))+'|'+str(t.get('start_time')),'need':0.5 if p=='gross' else 1})
    for t in school:
        p=pitch(t.get('spielstaette'))
        if not p:continue
        s,e=minutes(t['uhrzeit_von']),minutes(t['uhrzeit_bis'])
        if e>s:events.append({'type':'Fußballschule','pitch':p,'s':s,'e':e,'name':str(t.get('titel') or 'Fußballschule'),'key':str(t.get('id') or t.get('titel')),'need':0.5 if p=='gross' else 1})
    return events

def conflicts(day,events):
    out=[]
    for game in (e for e in events if e['type']=='Spiel'):
        # Zeitsegmente: alle parallelen Trainingseinheiten auf demselben Platz kumulieren.
        others=[e for e in events if e['type']!='Spiel' and e['pitch']==game['pitch'] and e['s']<game['e'] and e['e']>game['s']]
        boundaries=sorted({game['s'],game['e'],*(max(game['s'],e['s']) for e in others),*(min(game['e'],e['e']) for e in others)})
        affected=set()
        for a,b in zip(boundaries,boundaries[1:]):
            if a>=b:continue
            active=[e for e in others if e['s']<b and e['e']>a]
            if game['need']+sum(e['need'] for e in active)>1.00001:
                affected.update(id(e) for e in active)
        for e in others:
            if id(e) not in affected:continue
            start,end=max(game['s'],e['s']),min(game['e'],e['e'])
            identity=f"{day}|{game['pitch']}|{game['key']}|{game['s']}|{e['type']}|{e['key']}|{e['s']}"
            out.append({'id':hashlib.sha256(identity.encode()).hexdigest(),'day':day,'pitch':game['pitch'],'game':game['name'],'game_time':fmt(game['s'])+'–'+fmt(game['e']),'activity':e['type']+': '+e['name'],'activity_time':fmt(e['s'])+'–'+fmt(e['e']),'overlap':fmt(start)+'–'+fmt(end)})
    return out

def notify(items):
    msg=EmailMessage()
    msg['From']=os.environ['SMTP_USER']
    msg['To']=os.environ.get('ALERT_TO','ugur.altan@outlook.de')
    msg['Subject']=f"Mein FSV: {len(items)} neue Platzkonflikt(e)"
    names={'gross':'Nieder-Olm KR (großer Kunstrasen)','kleinfeld':'Nieder-Olm Kleinfeld KR','rasen':'Nieder-Olm RA'}
    lines=['Automatische Platzkonfliktprüfung – Mein FSV','Zeitraum: heute bis einschließlich 14 Tage','']
    for i,c in enumerate(items,1):
        lines.extend([f"{i}. {c['day']} · {names.get(c['pitch'],c['pitch'])}",f"Spiel: {c['game']} ({c['game_time']})",f"Betroffen: {c['activity']} ({c['activity_time']})",f"Zeitliche Überschneidung: {c['overlap']}",''])
    lines.append('Hinweis: Normale Spiele werden wie in der Spielstättenansicht mit 60 Minuten angesetzt; E-/F-Spiele auf dem großen Kunstrasen mit halbem Platzbedarf.')
    msg.set_content('\n'.join(lines))
    with smtplib.SMTP_SSL(os.environ.get('SMTP_HOST','smtp.strato.de'),int(os.environ.get('SMTP_PORT','465')),context=ssl.create_default_context(),timeout=30) as server:
        server.login(os.environ['SMTP_USER'],os.environ['SMTP_PASSWORD'])
        server.send_message(msg)

def main():
    now=datetime.now(TZ)
    if os.environ.get('GITHUB_EVENT_NAME')=='schedule' and now.hour not in (12,18):
        print('Außerhalb 12/18 Uhr Europe/Berlin – Zeitumstellungsschutz.');return
    today=now.date()
    found=[]
    for i in range(15):
        day=(today+timedelta(days=i)).isoformat()
        found.extend(conflicts(day,fetch_day(day)))
    print(f'{len(found)} Konflikte gefunden.')
    if not found:return
    # SQL-Setup der Tabelle ist erforderlich; bei Fehlern nicht blind versenden.
    existing=api('/platzkonflikt_meldungen?select=konflikt_id&konflikt_id=in.('+','.join(c['id'] for c in found)+')')
    known={r['konflikt_id'] for r in existing}
    new=[c for c in found if c['id'] not in known]
    print(f'{len(new)} neue Konflikte.')
    if not new:return
    notify(new)
    api('/platzkonflikt_meldungen',method='POST',body=[{'konflikt_id':c['id']} for c in new],extra={'Prefer':'return=minimal,resolution=ignore-duplicates'})
    print(f'E-Mail an {os.environ.get("ALERT_TO","ugur.altan@outlook.de")} versendet; Meldungen gespeichert.')

if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(f'FEHLER: {exc}',file=sys.stderr);sys.exit(1)
