from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import json, os, random, string, threading, secrets, socket, time

HOST='0.0.0.0'
PORT=int(os.environ.get('PORT','8765'))
BASE=os.path.dirname(os.path.abspath(__file__))
PUBLIC=os.path.join(BASE,'public')
ROOMS={}
LOCK=threading.RLock()

SUITS=['H','D','C','S']
RANKS=['6','7','8','9','10','J','Q','K','A']
SCORE={'6':0,'7':0,'8':0,'9':0,'10':10,'J':25,'Q':10,'K':10,'A':15}
SUIT_SYMBOL={'H':'♥','D':'♦','C':'♣','S':'♠'}


def now(): return time.time()
def card_id(rank,suit): return f'{rank}{suit}'
def new_deck():
    d=[card_id(r,s) for s in SUITS for r in RANKS]
    random.shuffle(d); return d

def rank(c): return c[:-1]
def suit(c): return c[-1]
def hand_score(hand): return sum(SCORE[rank(c)] for c in hand)
def room_code():
    for _ in range(100):
        c=''.join(random.choice(string.ascii_uppercase+string.digits) for _ in range(6))
        if c not in ROOMS: return c
    raise RuntimeError('room code exhaustion')

def next_seat(room,p,steps=1): return (p+steps)%len(room['players'])

def add_log(room,msg):
    room['log'].append({'t':int(now()),'msg':msg})
    room['log']=room['log'][-80:]
    room['version']+=1

def recycle(room):
    if room['deck']: return True
    if len(room['discard'])<=1: return False
    top=room['discard'].pop()
    room['deck']=room['discard'][:]
    random.shuffle(room['deck'])
    room['discard']=[top]
    add_log(room,'🔄 Сброс перемешан и снова стал колодой.')
    return bool(room['deck'])

def draw_one(room):
    if not room['deck'] and not recycle(room): return None
    return room['deck'].pop() if room['deck'] else None

def draw_penalty(room,p,n):
    got=0
    for _ in range(n):
        c=draw_one(room)
        if not c: break
        room['players'][p]['hand'].append(c); got+=1
    return got

def current_series(room):
    if not room['discard']: return []
    r=rank(room['discard'][-1]); out=[]
    for c in reversed(room['discard']):
        if rank(c)!=r: break
        out.append(c)
    return list(reversed(out))

def current_bridge(room):
    s=current_series(room)
    return rank(s[-1]) if len(s)>=4 and rank(s[-1])!='6' else None

def legal_cards(room,p):
    if not room['started'] or room['match_over'] or room['round_over']: return []
    if room['current']!=p or room['pending_bridge'] is not None or room['awaiting_suit'] is not None: return []
    hand=room['players'][p]['hand']
    a=room['action']
    if not a or a['player']!=p: return []
    if a['forced_six']:
        last6=None
        for c in reversed(a['cards']):
            if rank(c)=='6': last6=c; break
        if not last6: return []
        return [c for c in hand if rank(c)=='6' or suit(c)==suit(last6)]
    if a['cards']:
        return [c for c in hand if rank(c)==a['combo_rank']]
    top=room['discard'][-1]
    if room['active_suit']:
        return [c for c in hand if rank(c)=='J' or suit(c)==room['active_suit']]
    return [c for c in hand if rank(c)==rank(top) or suit(c)==suit(top)]

def begin_action(room,p,pre=None):
    room['action']={
        'player':p,'cards':[pre] if pre else [],
        'combo_rank':rank(pre) if pre else None,
        'forced_six':bool(pre and rank(pre)=='6'),
        'bridge_declared':False,
        'suit_chosen':False,
    }
    if pre and rank(pre)!='J': room['active_suit']=None

def choose_dealer(room):
    contenders=list(range(len(room['players'])))
    while len(contenders)>1:
        rolls={p:random.randint(1,6) for p in contenders}
        m=max(rolls.values())
        add_log(room,'🎲 '+', '.join(f"{room['players'][p]['name']}={rolls[p]}" for p in contenders))
        contenders=[p for p,v in rolls.items() if v==m]
        if len(contenders)>1: add_log(room,'🎲 Ничья по максимуму — переброс между лидерами.')
    return contenders[0]

def start_round(room,first=False):
    room['deck']=new_deck(); room['discard']=[]; room['active_suit']=None
    room['pending_bridge']=None; room['awaiting_suit']=None; room['round_over']=False
    room['round_summary']=None; room['no_move_count']=0
    for p in room['players']: p['hand']=[]
    for _ in range(5):
        for p in range(len(room['players'])):
            room['players'][p]['hand'].append(room['deck'].pop())
    firstcard=room['players'][room['dealer']]['hand'].pop()
    room['discard'].append(firstcard)
    room['current']=room['dealer']
    begin_action(room,room['dealer'],firstcard)
    add_log(room,f"🃏 Раунд {room['round']}. Раздаёт {room['players'][room['dealer']]['name']}. Первая карта {show_card(firstcard)}.")

def start_match(room):
    room['scores']=[0]*len(room['players']); room['round']=1; room['match_over']=False
    room['dealer']=choose_dealer(room); room['started']=True; start_round(room,True)

def show_card(c): return rank(c)+SUIT_SYMBOL[suit(c)]

def apply_card(room,p,c):
    a=room['action']
    was_forced=a['forced_six']
    last6=None
    if was_forced:
        for x in reversed(a['cards']):
            if rank(x)=='6': last6=x; break
    room['players'][p]['hand'].remove(c)
    room['discard'].append(c); a['cards'].append(c)
    if len(a['cards'])==1: a['combo_rank']=rank(c)
    if was_forced:
        if rank(c)=='6':
            a['forced_six']=True; a['combo_rank']='6'
        elif last6 and suit(c)==suit(last6):
            a['forced_six']=False; a['combo_rank']=rank(c)
    elif rank(c)=='6' and len(a['cards'])==1:
        a['forced_six']=True; a['combo_rank']='6'
    if len(a['cards'])==1 and rank(c)!='J' and room['active_suit']:
        room['active_suit']=None
    add_log(room,f"{room['players'][p]['name']} кладёт {show_card(c)}.")
    if current_bridge(room):
        room['pending_bridge']=p
        add_log(room,f"🌉 Четыре карты {rank(c)} подряд — {room['players'][p]['name']} может объявить БРИДЖ.")


def resolve_action(room,p):
    a=room['action']; cards=a['cards'][:]
    c7=sum(1 for c in cards if rank(c)=='7')
    c8=sum(1 for c in cards if rank(c)=='8')
    ca=sum(1 for c in cards if rank(c)=='A')
    cj=sum(1 for c in cards if rank(c)=='J')

    # Ending condition comes before Jack suit choice.
    empty=(len(room['players'][p]['hand'])==0)
    bridge=a.get('bridge_declared',False)
    if empty or bridge:
        end_round(room,'БРИДЖ' if bridge else 'все карты сброшены',p,cj*25)
        return

    # A Jack action needs a suit before effects/turn transfer.
    if cj>0 and room['awaiting_suit'] is None and not a.get('suit_chosen',False):
        room['awaiting_suit']=p
        return

    if c7:
        t=next_seat(room,p)
        got=draw_penalty(room,t,c7)
        add_log(room,f"{room['players'][t]['name']} получает +{got} за {c7} семёрк(и) и всё равно ходит.")

    opponents=[next_seat(room,p,i) for i in range(1,len(room['players']))]
    skipped=set()
    if c8:
        dist={}
        for i in range(c8):
            t=opponents[i%len(opponents)]
            got=draw_penalty(room,t,2)
            dist[t]=dist.get(t,0)+got; skipped.add(t)
        add_log(room,'8: '+', '.join(f"{room['players'][t]['name']} +{n}" for t,n in dist.items())+'; затронутые пропускают ход.')
    if ca:
        for i in range(min(ca,len(opponents))): skipped.add(opponents[i])
        add_log(room,f"Тузы: {len(skipped)} игрок(а) пропускают ход.")

    q=next_seat(room,p)
    guard=0
    while q in skipped and guard<len(room['players']):
        q=next_seat(room,q); guard+=1
    if guard>=len(room['players']): q=p
    room['action']=None; room['current']=q; room['no_move_count']=0; begin_action(room,q)
    room['awaiting_suit']=None
    room['version']+=1


def end_round(room,reason,finisher,jack_bonus):
    deltas=[hand_score(p['hand']) for p in room['players']]
    if finisher is not None: deltas[finisher]-=jack_bonus
    resets=[False]*len(room['players'])
    for i,d in enumerate(deltas):
        room['scores'][i]+=d
        if room['scores'][i]==110:
            room['scores'][i]=0; resets[i]=True
    game_over=any(s>=120 for s in room['scores'])
    winners=[]
    if game_over:
        m=min(room['scores']); winners=[i for i,s in enumerate(room['scores']) if s==m]
        room['match_over']=True
    room['round_over']=True
    room['round_summary']={'reason':reason,'finisher':finisher,'deltas':deltas,'totals':room['scores'][:],'resets':resets,'game_over':game_over,'winners':winners}
    room['history'].append({'round':room['round'],'reason':reason,'deltas':deltas,'totals':room['scores'][:],'resets':resets})
    room['action']=None; room['pending_bridge']=None; room['awaiting_suit']=None
    add_log(room,'🏁 Раунд окончен: '+reason+'.')


def api_state(room,pi):
    me=room['players'][pi]
    legal=set(legal_cards(room,pi))
    players=[]
    for i,p in enumerate(room['players']):
        players.append({'seat':i,'name':p['name'],'cards':len(p['hand']),'score':room['scores'][i] if i<len(room['scores']) else 0,'host':i==0})
    a=room['action'] if room['started'] else None
    return {
        'room':room['code'],'max_players':room['max_players'],'started':room['started'],'round':room['round'],'dealer':room['dealer'],
        'current':room['current'],'match_over':room['match_over'],'round_over':room['round_over'],'players':players,
        'me':pi,'hand':sorted(me['hand'],key=lambda c:(RANKS.index(rank(c)),SUITS.index(suit(c)))) if room['started'] else [],
        'legal':list(legal),'deck_count':len(room['deck']),'top':room['discard'][-1] if room['discard'] else None,
        'series':current_series(room) if room['started'] else [],'active_suit':room['active_suit'],'action':a,
        'pending_bridge':room['pending_bridge']==pi,'awaiting_suit':room['awaiting_suit']==pi,
        'history':room['history'][-30:],'log':list(reversed(room['log'][-30:])), 'round_summary':room['round_summary'], 'version':room['version']
    }


def auth(room,token):
    for i,p in enumerate(room['players']):
        if p['token']==token: return i
    return None

class Handler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args): pass
    def translate_path(self,path):
        parsed=urlparse(path); rel=parsed.path.lstrip('/') or 'index.html'
        full=os.path.abspath(os.path.join(PUBLIC,rel))
        if not full.startswith(os.path.abspath(PUBLIC)): return PUBLIC
        return full
    def send_json(self,obj,status=200):
        data=json.dumps(obj,ensure_ascii=False).encode('utf-8')
        self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
    def read_json(self):
        n=int(self.headers.get('Content-Length','0') or 0)
        return json.loads(self.rfile.read(n) or b'{}')
    def do_GET(self):
        u=urlparse(self.path)
        if u.path=='/healthz':
            return self.send_json({'ok':True,'service':'bridge-rubik'})
        if u.path=='/api/state':
            q=parse_qs(u.query); code=(q.get('room',[''])[0]).upper(); token=q.get('token',[''])[0]
            with LOCK:
                room=ROOMS.get(code)
                if not room: return self.send_json({'error':'Комната не найдена'},404)
                pi=auth(room,token)
                if pi is None: return self.send_json({'error':'Нет доступа'},403)
                return self.send_json(api_state(room,pi))
        return super().do_GET()
    def do_POST(self):
        try: body=self.read_json()
        except: return self.send_json({'error':'Некорректный JSON'},400)
        path=urlparse(self.path).path
        with LOCK:
            try:
                if path=='/api/create':
                    name=str(body.get('name','Рубик')).strip()[:24] or 'Игрок'
                    seats=max(2,min(4,int(body.get('seats',2))))
                    code=room_code(); token=secrets.token_urlsafe(24)
                    room={'code':code,'max_players':seats,'players':[{'name':name,'token':token,'hand':[]}], 'scores':[0], 'started':False,'round':1,'dealer':0,'current':0,'deck':[],'discard':[],'active_suit':None,'action':None,'pending_bridge':None,'awaiting_suit':None,'round_over':False,'round_summary':None,'match_over':False,'history':[],'log':[],'version':1,'created':now()}
                    ROOMS[code]=room; add_log(room,f'🚪 {name} создал комнату {code}.')
                    return self.send_json({'room':code,'token':token,'seat':0})
                if path=='/api/join':
                    code=str(body.get('room','')).upper().strip(); name=str(body.get('name','Игрок')).strip()[:24] or 'Игрок'; room=ROOMS.get(code)
                    if not room: return self.send_json({'error':'Комната не найдена'},404)
                    if room['started']: return self.send_json({'error':'Игра уже началась'},409)
                    if len(room['players'])>=room['max_players']: return self.send_json({'error':'Комната заполнена'},409)
                    token=secrets.token_urlsafe(24); room['players'].append({'name':name,'token':token,'hand':[]}); room['scores'].append(0); add_log(room,f'➕ {name} вошёл в комнату.')
                    return self.send_json({'room':code,'token':token,'seat':len(room['players'])-1})

                code=str(body.get('room','')).upper(); token=str(body.get('token','')); room=ROOMS.get(code)
                if not room: return self.send_json({'error':'Комната не найдена'},404)
                pi=auth(room,token)
                if pi is None: return self.send_json({'error':'Нет доступа'},403)

                if path=='/api/start':
                    if pi!=0: return self.send_json({'error':'Только хозяин запускает игру'},403)
                    if room['started']: return self.send_json({'error':'Уже запущено'},409)
                    if len(room['players'])!=room['max_players']: return self.send_json({'error':f"Нужно {room['max_players']} игроков"},409)
                    start_match(room); return self.send_json({'ok':True})
                if path=='/api/play':
                    c=str(body.get('card',''))
                    if room['current']!=pi: return self.send_json({'error':'Сейчас не ваш ход'},409)
                    if c not in legal_cards(room,pi): return self.send_json({'error':'Этой картой сейчас ходить нельзя'},409)
                    apply_card(room,pi,c); return self.send_json({'ok':True})
                if path=='/api/draw':
                    if room['current']!=pi: return self.send_json({'error':'Сейчас не ваш ход'},409)
                    if room['pending_bridge'] is not None or room['awaiting_suit'] is not None: return self.send_json({'error':'Сначала завершите текущее решение'},409)
                    a=room['action']; legal=legal_cards(room,pi)
                    if legal: return self.send_json({'error':'У вас есть допустимая карта'},409)
                    if a['cards'] and not a['forced_six']: return self.send_json({'error':'После обычного действия добор запрещён'},409)
                    count=0
                    while not legal_cards(room,pi):
                        c=draw_one(room)
                        if not c:
                            end_round(room,'тупик',None,0); break
                        room['players'][pi]['hand'].append(c); count+=1
                    if count: add_log(room,f"{room['players'][pi]['name']} добирает {count} карт до первой подходящей.")
                    return self.send_json({'ok':True,'count':count})
                if path=='/api/end':
                    if room['current']!=pi or not room['action'] or room['action']['player']!=pi: return self.send_json({'error':'Нечего завершать'},409)
                    if room['action']['forced_six']: return self.send_json({'error':'Сначала закройте шестёрку'},409)
                    if not room['action']['cards']: return self.send_json({'error':'Вы ещё не сыграли карту'},409)
                    resolve_action(room,pi); return self.send_json({'ok':True})
                if path=='/api/suit':
                    s=str(body.get('suit','')).upper()
                    if room['awaiting_suit']!=pi or s not in SUITS: return self.send_json({'error':'Сейчас нельзя выбирать масть'},409)
                    room['active_suit']=s; room['awaiting_suit']=None; room['action']['suit_chosen']=True; add_log(room,f"{room['players'][pi]['name']} заказывает масть {SUIT_SYMBOL[s]}."); resolve_action(room,pi); return self.send_json({'ok':True})
                if path=='/api/bridge':
                    if room['pending_bridge']!=pi: return self.send_json({'error':'Сейчас нет решения по Бриджу'},409)
                    dec=bool(body.get('declare',False)); room['pending_bridge']=None
                    if dec:
                        room['action']['bridge_declared']=True; add_log(room,f"🌉 {room['players'][pi]['name']} объявляет БРИДЖ!"); resolve_action(room,pi)
                    else:
                        add_log(room,f"{room['players'][pi]['name']} отказывается от Бриджа."); room['version']+=1
                    return self.send_json({'ok':True})
                if path=='/api/next_round':
                    if pi!=0: return self.send_json({'error':'Только хозяин'},403)
                    if not room['round_over'] or room['match_over']: return self.send_json({'error':'Сейчас нельзя начать следующий раунд'},409)
                    room['round']+=1; room['dealer']=next_seat(room,room['dealer']); start_round(room); return self.send_json({'ok':True})
                if path=='/api/new_match':
                    if pi!=0: return self.send_json({'error':'Только хозяин'},403)
                    start_match(room); return self.send_json({'ok':True})
                return self.send_json({'error':'Неизвестная команда'},404)
            except Exception as e:
                print('API ERROR',path,e,flush=True)
                return self.send_json({'error':'Ошибка сервера: '+str(e)},500)

if __name__=='__main__':
    os.chdir(PUBLIC)
    print('\n=== БРИДЖ РУБИКА — ONLINE SERVER ===', flush=True)
    print(f'Listening on {HOST}:{PORT}', flush=True)
    ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
