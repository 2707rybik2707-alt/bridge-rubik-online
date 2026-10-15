from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import json, os, random, string, threading, secrets, time

HOST = '0.0.0.0'
PORT = int(os.environ.get('PORT', '8765'))
BASE = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(BASE, 'public')
ROOMS = {}
LOCK = threading.RLock()

SUITS = ['H', 'D', 'C', 'S']
RANKS = ['6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
SCORE = {'6': 0, '7': 0, '8': 0, '9': 0, '10': 10, 'J': 25, 'Q': 10, 'K': 10, 'A': 15}
SUIT_SYMBOL = {'H': '♥', 'D': '♦', 'C': '♣', 'S': '♠'}
BOT_LEVELS = {'easy', 'medium', 'hard'}
BOT_DELAY = 0.60


def now(): return time.time()
def card_id(rank, suit): return f'{rank}{suit}'
def rank(c): return c[:-1]
def suit(c): return c[-1]
def show_card(c): return rank(c) + SUIT_SYMBOL[suit(c)]
def hand_score(hand): return sum(SCORE[rank(c)] for c in hand)

def new_deck():
    d = [card_id(r, s) for s in SUITS for r in RANKS]
    random.shuffle(d)
    return d

def room_code():
    for _ in range(200):
        c = ''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(6))
        if c not in ROOMS:
            return c
    raise RuntimeError('room code exhaustion')

def next_seat(room, p, steps=1):
    return (p + steps) % len(room['players'])

def add_log(room, msg):
    room['log'].append({'t': int(now()), 'msg': msg})
    room['log'] = room['log'][-100:]
    room['version'] += 1

def schedule_bot(room, delay=BOT_DELAY):
    if room.get('started') and not room.get('round_over') and not room.get('match_over'):
        p = room.get('current', 0)
        if 0 <= p < len(room['players']) and room['players'][p].get('is_bot'):
            room['bot_due'] = now() + delay
        else:
            room['bot_due'] = None
    else:
        room['bot_due'] = None

def recycle(room):
    if room['deck']:
        return True
    if len(room['discard']) <= 1:
        return False
    top = room['discard'].pop()
    room['deck'] = room['discard'][:]
    random.shuffle(room['deck'])
    room['discard'] = [top]
    add_log(room, '🔄 Сброс перемешан и снова стал колодой.')
    return bool(room['deck'])

def draw_one(room):
    if not room['deck'] and not recycle(room):
        return None
    return room['deck'].pop() if room['deck'] else None

def draw_penalty(room, p, n):
    got = 0
    for _ in range(n):
        c = draw_one(room)
        if not c:
            break
        room['players'][p]['hand'].append(c)
        got += 1
    return got

def current_series(room):
    if not room['discard']:
        return []
    r = rank(room['discard'][-1])
    out = []
    for c in reversed(room['discard']):
        if rank(c) != r:
            break
        out.append(c)
    return list(reversed(out))

def current_bridge(room):
    s = current_series(room)
    return rank(s[-1]) if len(s) >= 4 and rank(s[-1]) != '6' else None

def legal_cards(room, p):
    if not room['started'] or room['match_over'] or room['round_over']:
        return []
    if room['current'] != p or room['pending_bridge'] is not None or room['awaiting_suit'] is not None:
        return []
    hand = room['players'][p]['hand']
    a = room['action']
    if not a or a['player'] != p:
        return []
    if a['forced_six']:
        last6 = None
        for c in reversed(a['cards']):
            if rank(c) == '6':
                last6 = c
                break
        if not last6:
            return []
        return [c for c in hand if rank(c) == '6' or suit(c) == suit(last6)]
    if a['cards']:
        return [c for c in hand if rank(c) == a['combo_rank']]
    top = room['discard'][-1]
    if room['active_suit']:
        return [c for c in hand if rank(c) == 'J' or suit(c) == room['active_suit']]
    return [c for c in hand if rank(c) == rank(top) or suit(c) == suit(top)]

def begin_action(room, p, pre=None):
    room['action'] = {
        'player': p,
        'cards': [pre] if pre else [],
        'combo_rank': rank(pre) if pre else None,
        'forced_six': bool(pre and rank(pre) == '6'),
        'bridge_declared': False,
        'suit_chosen': False,
    }
    if pre and rank(pre) != 'J':
        room['active_suit'] = None

def choose_dealer(room):
    contenders = list(range(len(room['players'])))
    while len(contenders) > 1:
        rolls = {p: random.randint(1, 6) for p in contenders}
        m = max(rolls.values())
        add_log(room, '🎲 ' + ', '.join(f"{room['players'][p]['name']}={rolls[p]}" for p in contenders))
        contenders = [p for p, v in rolls.items() if v == m]
        if len(contenders) > 1:
            add_log(room, '🎲 Ничья по максимуму — переброс только между лидерами.')
    return contenders[0]

def start_round(room):
    room['deck'] = new_deck()
    room['discard'] = []
    room['active_suit'] = None
    room['pending_bridge'] = None
    room['awaiting_suit'] = None
    room['round_over'] = False
    room['round_summary'] = None
    room['no_move_count'] = 0
    room['bot_due'] = None
    for p in room['players']:
        p['hand'] = []
    for _ in range(5):
        for i in range(len(room['players'])):
            room['players'][i]['hand'].append(room['deck'].pop())
    first = room['players'][room['dealer']]['hand'].pop()
    room['discard'].append(first)
    room['current'] = room['dealer']
    begin_action(room, room['dealer'], first)
    add_log(room, f"🃏 Раунд {room['round']}. Раздаёт {room['players'][room['dealer']]['name']}. Первая карта {show_card(first)}.")
    schedule_bot(room, 0.8)

def start_match(room):
    room['scores'] = [0] * len(room['players'])
    room['round'] = 1
    room['match_over'] = False
    room['dealer'] = choose_dealer(room)
    room['started'] = True
    start_round(room)

def apply_card(room, p, c):
    a = room['action']
    was_forced = a['forced_six']
    last6 = None
    if was_forced:
        for x in reversed(a['cards']):
            if rank(x) == '6':
                last6 = x
                break
    room['players'][p]['hand'].remove(c)
    room['discard'].append(c)
    a['cards'].append(c)
    if len(a['cards']) == 1:
        a['combo_rank'] = rank(c)
    if was_forced:
        if rank(c) == '6':
            a['forced_six'] = True
            a['combo_rank'] = '6'
        elif last6 and suit(c) == suit(last6):
            a['forced_six'] = False
            a['combo_rank'] = rank(c)
    elif rank(c) == '6' and len(a['cards']) == 1:
        a['forced_six'] = True
        a['combo_rank'] = '6'
    if len(a['cards']) == 1 and rank(c) != 'J' and room['active_suit']:
        room['active_suit'] = None
    add_log(room, f"{room['players'][p]['name']} кладёт {show_card(c)}.")
    if current_bridge(room):
        room['pending_bridge'] = p
        add_log(room, f"🌉 Четыре карты {rank(c)} подряд — {room['players'][p]['name']} решает: БРИДЖ или продолжать.")
    schedule_bot(room)

def distribute_specials(room, p, cards):
    c7 = sum(1 for c in cards if rank(c) == '7')
    c8 = sum(1 for c in cards if rank(c) == '8')
    ca = sum(1 for c in cards if rank(c) == 'A')
    if c7:
        t = next_seat(room, p)
        got = draw_penalty(room, t, c7)
        add_log(room, f"{room['players'][t]['name']} получает +{got} за {c7} семёрк(и) и всё равно ходит.")
    opponents = [next_seat(room, p, i) for i in range(1, len(room['players']))]
    skipped = set()
    if c8 and opponents:
        dist = {}
        for i in range(c8):
            t = opponents[i % len(opponents)]
            got = draw_penalty(room, t, 2)
            dist[t] = dist.get(t, 0) + got
            skipped.add(t)
        add_log(room, '8: ' + ', '.join(f"{room['players'][t]['name']} +{n}" for t, n in dist.items()) + '; затронутые пропускают по одному ходу.')
    if ca and opponents:
        for i in range(min(ca, len(opponents))):
            skipped.add(opponents[i])
        add_log(room, f"Тузы: следующие {min(ca, len(opponents))} игрок(а) пропускают ход.")
    return skipped

def resolve_action(room, p):
    a = room['action']
    cards = a['cards'][:]
    cj = sum(1 for c in cards if rank(c) == 'J')
    empty = len(room['players'][p]['hand']) == 0
    bridge = a.get('bridge_declared', False)
    terminal = empty or bridge

    # Валет требует масть только если раунд НЕ заканчивается.
    if not terminal and cj > 0 and room['awaiting_suit'] is None and not a.get('suit_chosen', False):
        room['awaiting_suit'] = p
        room['version'] += 1
        schedule_bot(room)
        return

    # Штрафы 7/8 срабатывают даже если этой комбинацией игрок завершает раунд/Бридж.
    skipped = distribute_specials(room, p, cards)

    if terminal:
        end_round(room, 'БРИДЖ' if bridge else 'все карты сброшены', p, cj * 25)
        return

    q = next_seat(room, p)
    guard = 0
    while q in skipped and guard < len(room['players']):
        q = next_seat(room, q)
        guard += 1
    if guard >= len(room['players']):
        q = p
    room['action'] = None
    room['current'] = q
    room['no_move_count'] = 0
    room['awaiting_suit'] = None
    begin_action(room, q)
    room['version'] += 1
    schedule_bot(room)

def end_round(room, reason, finisher, jack_bonus):
    deltas = [hand_score(p['hand']) for p in room['players']]
    if finisher is not None:
        deltas[finisher] -= jack_bonus
    resets = [False] * len(room['players'])
    for i, d in enumerate(deltas):
        room['scores'][i] += d
        if room['scores'][i] == 110:
            room['scores'][i] = 0
            resets[i] = True
    game_over = any(s >= 120 for s in room['scores'])
    winners = []
    if game_over:
        m = min(room['scores'])
        winners = [i for i, s in enumerate(room['scores']) if s == m]
        room['match_over'] = True
    room['round_over'] = True
    room['round_summary'] = {
        'reason': reason,
        'finisher': finisher,
        'deltas': deltas,
        'totals': room['scores'][:],
        'resets': resets,
        'game_over': game_over,
        'winners': winners,
    }
    room['history'].append({
        'round': room['round'],
        'reason': reason,
        'deltas': deltas,
        'totals': room['scores'][:],
        'resets': resets,
    })
    room['action'] = None
    room['pending_bridge'] = None
    room['awaiting_suit'] = None
    room['bot_due'] = None
    add_log(room, '🏁 Раунд окончен: ' + reason + '.')

def choose_bot_suit(room, p):
    counts = {s: 0 for s in SUITS}
    for c in room['players'][p]['hand']:
        counts[suit(c)] += 1
    return max(SUITS, key=lambda s: (counts[s], random.random()))

def bot_bridge_decision(room, p):
    pl = room['players'][p]
    level = pl.get('difficulty', 'medium')
    a = room['action'] or {'cards': []}
    jbonus = sum(1 for c in a['cards'] if rank(c) == 'J') * 25
    result = hand_score(pl['hand']) - jbonus
    projected = room['scores'][p] + result
    opp_min = min((len(x['hand']) for i, x in enumerate(room['players']) if i != p), default=99)
    if projected == 110:
        return True
    if projected >= 120:
        return False
    if result <= 0:
        return True
    if result >= 40:
        return False
    if level == 'easy':
        return len(pl['hand']) <= 2 and result <= 25
    if level == 'medium':
        return result <= 15 or (opp_min <= 1 and result <= 25) or (len(pl['hand']) <= 2 and result <= 30)
    # hard
    if result <= 10:
        return True
    if opp_min <= 1 and result <= 30:
        return True
    if len(pl['hand']) <= 2 and result <= 30:
        return True
    # если бот сейчас далеко впереди по счёту, может принять небольшой плюс ради безопасного закрытия
    others = [room['scores'][i] for i in range(len(room['players'])) if i != p]
    if others and room['scores'][p] + result < min(others) and result <= 25:
        return True
    return False

def bot_should_add(room, p):
    pl = room['players'][p]
    level = pl.get('difficulty', 'medium')
    a = room['action']
    if not a or not a['cards'] or a['forced_six']:
        return True
    extras = legal_cards(room, p)
    if not extras:
        return False
    r = a['combo_rank']
    if level == 'easy':
        return random.random() < (0.55 if r in ('7', '8', 'A', 'J') else 0.35)
    if level == 'medium':
        return random.random() < (0.82 if r in ('7', '8', 'A', 'J') else 0.58)
    # hard: почти всегда усиливает спецкомбинацию, обычные — если это помогает уменьшить руку
    if r in ('7', '8', 'A', 'J'):
        return True
    return len(pl['hand']) <= 5 or random.random() < 0.72

def choose_bot_card(room, p, cards):
    pl = room['players'][p]
    level = pl.get('difficulty', 'medium')
    a = room['action']
    if level == 'easy':
        return random.choice(cards)
    def weight(c):
        w = SCORE[rank(c)]
        if rank(c) == '8': w += 32
        elif rank(c) == 'A': w += 27
        elif rank(c) == 'J': w += 24
        elif rank(c) == '7': w += 18
        elif rank(c) == '6': w += 4
        if a and a.get('forced_six') and rank(c) != '6':
            w += 15
        if level == 'hard':
            # избавляться от дорогих карт полезно; при равенстве выбираем масть, которой больше на руке
            same_suit = sum(1 for x in pl['hand'] if suit(x) == suit(c))
            w += same_suit * 1.4
        return w + random.random() * 2
    return max(cards, key=weight)

def draw_until_legal(room, p):
    count = 0
    while not legal_cards(room, p):
        c = draw_one(room)
        if not c:
            end_round(room, 'тупик', None, 0)
            return count, False
        room['players'][p]['hand'].append(c)
        count += 1
    if count:
        add_log(room, f"{room['players'][p]['name']} добирает {count} карт до первой подходящей.")
    return count, True

def bot_step(room):
    if not room['started'] or room['round_over'] or room['match_over']:
        return
    p = room['current']
    if not room['players'][p].get('is_bot'):
        room['bot_due'] = None
        return

    # Бридж
    if room['pending_bridge'] == p:
        dec = bot_bridge_decision(room, p)
        room['pending_bridge'] = None
        if dec:
            room['action']['bridge_declared'] = True
            add_log(room, f"🌉 {room['players'][p]['name']} объявляет БРИДЖ!")
            resolve_action(room, p)
        else:
            add_log(room, f"{room['players'][p]['name']} отказывается от Бриджа и играет дальше.")
            schedule_bot(room)
        return

    # Валет — выбор масти
    if room['awaiting_suit'] == p:
        s = choose_bot_suit(room, p)
        room['active_suit'] = s
        room['awaiting_suit'] = None
        room['action']['suit_chosen'] = True
        add_log(room, f"{room['players'][p]['name']} заказывает масть {SUIT_SYMBOL[s]}.")
        resolve_action(room, p)
        return

    a = room['action']
    if not a or a['player'] != p:
        begin_action(room, p)
        a = room['action']

    legal = legal_cards(room, p)
    if not legal:
        if a['cards']:
            if a['forced_six']:
                _, ok = draw_until_legal(room, p)
                if ok:
                    schedule_bot(room)
                return
            resolve_action(room, p)
            return
        _, ok = draw_until_legal(room, p)
        if ok:
            schedule_bot(room)
        return

    if a['cards'] and not a['forced_six'] and not bot_should_add(room, p):
        resolve_action(room, p)
        return

    c = choose_bot_card(room, p, legal)
    apply_card(room, p, c)

def maybe_drive_bot(room):
    if not room.get('started') or room.get('round_over') or room.get('match_over'):
        return
    p = room.get('current', 0)
    if not room['players'][p].get('is_bot'):
        room['bot_due'] = None
        return
    if room.get('bot_due') is None:
        room['bot_due'] = now() + BOT_DELAY
        return
    if now() >= room['bot_due']:
        room['bot_due'] = None
        bot_step(room)
        if room.get('started') and not room.get('round_over') and not room.get('match_over'):
            p2 = room.get('current', 0)
            if room['players'][p2].get('is_bot'):
                schedule_bot(room)

def make_room(name, seats):
    code = room_code()
    token = secrets.token_urlsafe(24)
    room = {
        'code': code,
        'max_players': seats,
        'players': [{'name': name, 'token': token, 'hand': [], 'is_bot': False, 'difficulty': None}],
        'scores': [0],
        'started': False,
        'round': 1,
        'dealer': 0,
        'current': 0,
        'deck': [],
        'discard': [],
        'active_suit': None,
        'action': None,
        'pending_bridge': None,
        'awaiting_suit': None,
        'round_over': False,
        'round_summary': None,
        'match_over': False,
        'history': [],
        'log': [],
        'chat': [],
        'reactions': [],
        'reaction_seq': 0,
        'version': 1,
        'created': now(),
        'bot_due': None,
        'couple_mode': False,
        'couple_bot_difficulty': 'medium',
    }
    ROOMS[code] = room
    add_log(room, f'🚪 {name} создал комнату {code}.')
    return room, token

def bot_name(room):
    used = {p['name'] for p in room['players']}
    n = 1
    while f'Бот {n}' in used:
        n += 1
    return f'Бот {n}'

def api_state(room, pi):
    maybe_drive_bot(room)
    me = room['players'][pi]
    legal = set(legal_cards(room, pi))
    players = []
    for i, p in enumerate(room['players']):
        players.append({
            'seat': i,
            'name': p['name'],
            'cards': len(p['hand']),
            'score': room['scores'][i] if i < len(room['scores']) else 0,
            'host': i == 0,
            'is_bot': bool(p.get('is_bot')),
            'difficulty': p.get('difficulty'),
        })
    a = room['action'] if room['started'] else None
    return {
        'room': room['code'],
        'max_players': room['max_players'],
        'couple_mode': bool(room.get('couple_mode')),
        'started': room['started'],
        'round': room['round'],
        'dealer': room['dealer'],
        'current': room['current'],
        'match_over': room['match_over'],
        'round_over': room['round_over'],
        'players': players,
        'me': pi,
        'hand': sorted(me['hand'], key=lambda c: (RANKS.index(rank(c)), SUITS.index(suit(c)))) if room['started'] else [],
        'legal': list(legal),
        'deck_count': len(room['deck']),
        'top': room['discard'][-1] if room['discard'] else None,
        'series': current_series(room) if room['started'] else [],
        'active_suit': room['active_suit'],
        'action': a,
        'pending_bridge': room['pending_bridge'] == pi,
        'awaiting_suit': room['awaiting_suit'] == pi,
        'history': room['history'][-40:],
        'log': list(reversed(room['log'][-40:])),
        'chat': room.get('chat', [])[-60:],
        'reactions': room.get('reactions', [])[-30:],
        'round_summary': room['round_summary'],
        'version': room['version'],
    }

def auth(room, token):
    for i, p in enumerate(room['players']):
        if not p.get('is_bot') and p.get('token') == token:
            return i
    return None

class Handler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        super().end_headers()

    def translate_path(self, path):
        parsed = urlparse(path)
        rel = parsed.path.lstrip('/') or 'index.html'
        full = os.path.abspath(os.path.join(PUBLIC, rel))
        if not full.startswith(os.path.abspath(PUBLIC)):
            return PUBLIC
        return full

    def send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        n = int(self.headers.get('Content-Length', '0') or 0)
        return json.loads(self.rfile.read(n) or b'{}')

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == '/healthz':
            return self.send_json({'ok': True, 'service': 'bridge-rubik-full'})
        if u.path == '/api/state':
            q = parse_qs(u.query)
            code = (q.get('room', [''])[0]).upper()
            token = q.get('token', [''])[0]
            with LOCK:
                room = ROOMS.get(code)
                if not room:
                    return self.send_json({'error': 'Комната не найдена или уже закрыта'}, 404)
                pi = auth(room, token)
                if pi is None:
                    return self.send_json({'error': 'Нет доступа к комнате'}, 403)
                return self.send_json(api_state(room, pi))
        return super().do_GET()

    def do_POST(self):
        try:
            body = self.read_json()
        except Exception:
            return self.send_json({'error': 'Некорректный JSON'}, 400)
        path = urlparse(self.path).path
        with LOCK:
            try:
                if path == '/api/create_couple':
                    name = str(body.get('name', 'Рубик')).strip()[:24] or 'Рубик'
                    difficulty = str(body.get('difficulty', 'medium')).lower()
                    if difficulty not in BOT_LEVELS:
                        difficulty = 'medium'
                    room, token = make_room(name, 4)
                    room['couple_mode'] = True
                    room['couple_bot_difficulty'] = difficulty
                    # Место 1: хозяин. Место 2: первый бот.
                    # Место 3 зарезервировано для второго человека (напротив хозяина).
                    # Второй бот будет добавлен автоматически после входа партнёра.
                    room['players'].append({'name': 'Бот 1', 'token': None, 'hand': [], 'is_bot': True, 'difficulty': difficulty})
                    room['scores'].append(0)
                    add_log(room, f'❤️ Создан режим для двоих: {name} → Бот 1 → партнёр → Бот 2.')
                    return self.send_json({'room': room['code'], 'token': token, 'seat': 0, 'couple_mode': True})

                if path == '/api/create':
                    name = str(body.get('name', 'Рубик')).strip()[:24] or 'Игрок'
                    seats = max(2, min(4, int(body.get('seats', 2))))
                    room, token = make_room(name, seats)
                    return self.send_json({'room': room['code'], 'token': token, 'seat': 0})

                if path == '/api/join':
                    code = str(body.get('room', '')).upper().strip()
                    name = str(body.get('name', 'Игрок')).strip()[:24] or 'Игрок'
                    room = ROOMS.get(code)
                    if not room:
                        return self.send_json({'error': 'Комната не найдена'}, 404)
                    if room['started']:
                        return self.send_json({'error': 'Игра уже началась'}, 409)
                    if len(room['players']) >= room['max_players']:
                        return self.send_json({'error': 'Комната заполнена'}, 409)
                    token = secrets.token_urlsafe(24)
                    if room.get('couple_mode'):
                        # В couple-mode разрешён ровно один второй человек.
                        # До его входа список всегда: [хозяин, Бот 1].
                        if len(room['players']) != 2 or not room['players'][1].get('is_bot'):
                            return self.send_json({'error': 'Партнёр уже подключён или комната заполнена'}, 409)
                        room['players'].append({'name': name, 'token': token, 'hand': [], 'is_bot': False, 'difficulty': None})
                        room['scores'].append(0)
                        partner_seat = 2
                        difficulty = room.get('couple_bot_difficulty', 'medium')
                        room['players'].append({'name': 'Бот 2', 'token': None, 'hand': [], 'is_bot': True, 'difficulty': difficulty})
                        room['scores'].append(0)
                        add_log(room, f'❤️ {name} занял место напротив хозяина. Бот 2 добавлен автоматически.')
                        return self.send_json({'room': code, 'token': token, 'seat': partner_seat, 'couple_mode': True})
                    room['players'].append({'name': name, 'token': token, 'hand': [], 'is_bot': False, 'difficulty': None})
                    room['scores'].append(0)
                    add_log(room, f'➕ {name} вошёл в комнату.')
                    return self.send_json({'room': code, 'token': token, 'seat': len(room['players']) - 1})

                code = str(body.get('room', '')).upper()
                token = str(body.get('token', ''))
                room = ROOMS.get(code)
                if not room:
                    return self.send_json({'error': 'Комната не найдена или уже закрыта'}, 404)
                pi = auth(room, token)
                if pi is None:
                    return self.send_json({'error': 'Нет доступа'}, 403)

                if path == '/api/add_bot':
                    if room.get('couple_mode'):
                        return self.send_json({'error': 'В режиме для двоих боты расставляются автоматически'}, 409)
                    if pi != 0:
                        return self.send_json({'error': 'Только хозяин может добавлять ботов'}, 403)
                    if room['started']:
                        return self.send_json({'error': 'Игра уже началась'}, 409)
                    if len(room['players']) >= room['max_players']:
                        return self.send_json({'error': 'Все места заняты'}, 409)
                    difficulty = str(body.get('difficulty', 'medium')).lower()
                    if difficulty not in BOT_LEVELS:
                        difficulty = 'medium'
                    room['players'].append({'name': bot_name(room), 'token': None, 'hand': [], 'is_bot': True, 'difficulty': difficulty})
                    room['scores'].append(0)
                    add_log(room, f"🤖 Добавлен {room['players'][-1]['name']} ({difficulty}).")
                    return self.send_json({'ok': True})

                if path == '/api/remove_bot':
                    if room.get('couple_mode'):
                        return self.send_json({'error': 'В режиме для двоих боты расставляются автоматически'}, 409)
                    if pi != 0:
                        return self.send_json({'error': 'Только хозяин'}, 403)
                    if room['started']:
                        return self.send_json({'error': 'Игра уже началась'}, 409)
                    seat = int(body.get('seat', -1))
                    if seat <= 0 or seat >= len(room['players']) or not room['players'][seat].get('is_bot'):
                        return self.send_json({'error': 'Бот не найден'}, 404)
                    nm = room['players'][seat]['name']
                    room['players'].pop(seat)
                    room['scores'].pop(seat)
                    add_log(room, f'🗑️ {nm} удалён из комнаты.')
                    return self.send_json({'ok': True})

                if path == '/api/set_bot':
                    if pi != 0:
                        return self.send_json({'error': 'Только хозяин'}, 403)
                    if room['started']:
                        return self.send_json({'error': 'Игра уже началась'}, 409)
                    seat = int(body.get('seat', -1))
                    difficulty = str(body.get('difficulty', 'medium')).lower()
                    if difficulty not in BOT_LEVELS:
                        return self.send_json({'error': 'Неизвестная сложность'}, 400)
                    if seat <= 0 or seat >= len(room['players']) or not room['players'][seat].get('is_bot'):
                        return self.send_json({'error': 'Бот не найден'}, 404)
                    room['players'][seat]['difficulty'] = difficulty
                    room['version'] += 1
                    return self.send_json({'ok': True})

                if path == '/api/kick':
                    if pi != 0:
                        return self.send_json({'error': 'Только хозяин'}, 403)
                    if room['started']:
                        return self.send_json({'error': 'После старта убрать игрока нельзя'}, 409)
                    seat = int(body.get('seat', -1))
                    if seat <= 0 or seat >= len(room['players']) or room['players'][seat].get('is_bot'):
                        return self.send_json({'error': 'Игрок не найден'}, 404)
                    nm = room['players'][seat]['name']
                    room['players'].pop(seat)
                    room['scores'].pop(seat)
                    add_log(room, f'🚪 {nm} убран из комнаты хозяином.')
                    return self.send_json({'ok': True})

                if path == '/api/start':
                    if pi != 0:
                        return self.send_json({'error': 'Только хозяин запускает игру'}, 403)
                    if room['started']:
                        return self.send_json({'error': 'Уже запущено'}, 409)
                    if len(room['players']) != room['max_players']:
                        return self.send_json({'error': f"Нужно заполнить все {room['max_players']} места"}, 409)
                    start_match(room)
                    return self.send_json({'ok': True})

                if path == '/api/play':
                    c = str(body.get('card', ''))
                    if room['current'] != pi:
                        return self.send_json({'error': 'Сейчас не ваш ход'}, 409)
                    if c not in legal_cards(room, pi):
                        return self.send_json({'error': 'Этой картой сейчас ходить нельзя'}, 409)
                    apply_card(room, pi, c)
                    return self.send_json({'ok': True})

                if path == '/api/draw':
                    if room['current'] != pi:
                        return self.send_json({'error': 'Сейчас не ваш ход'}, 409)
                    if room['pending_bridge'] is not None or room['awaiting_suit'] is not None:
                        return self.send_json({'error': 'Сначала завершите текущее решение'}, 409)
                    a = room['action']
                    legal = legal_cards(room, pi)
                    if legal:
                        return self.send_json({'error': 'У вас есть допустимая карта'}, 409)
                    if a['cards'] and not a['forced_six']:
                        return self.send_json({'error': 'После обычного действия добор запрещён'}, 409)
                    count = 0
                    while not legal_cards(room, pi):
                        c = draw_one(room)
                        if not c:
                            end_round(room, 'тупик', None, 0)
                            break
                        room['players'][pi]['hand'].append(c)
                        count += 1
                    if count:
                        add_log(room, f"{room['players'][pi]['name']} добирает {count} карт до первой подходящей.")
                    return self.send_json({'ok': True, 'count': count})

                if path == '/api/end':
                    if room['current'] != pi or not room['action'] or room['action']['player'] != pi:
                        return self.send_json({'error': 'Нечего завершать'}, 409)
                    if room['action']['forced_six']:
                        return self.send_json({'error': 'Сначала закройте шестёрку'}, 409)
                    if not room['action']['cards']:
                        return self.send_json({'error': 'Вы ещё не сыграли карту'}, 409)
                    resolve_action(room, pi)
                    return self.send_json({'ok': True})

                if path == '/api/suit':
                    s = str(body.get('suit', '')).upper()
                    if room['awaiting_suit'] != pi or s not in SUITS:
                        return self.send_json({'error': 'Сейчас нельзя выбирать масть'}, 409)
                    room['active_suit'] = s
                    room['awaiting_suit'] = None
                    room['action']['suit_chosen'] = True
                    add_log(room, f"{room['players'][pi]['name']} заказывает масть {SUIT_SYMBOL[s]}.")
                    resolve_action(room, pi)
                    return self.send_json({'ok': True})

                if path == '/api/bridge':
                    if room['pending_bridge'] != pi:
                        return self.send_json({'error': 'Сейчас нет решения по Бриджу'}, 409)
                    dec = bool(body.get('declare', False))
                    room['pending_bridge'] = None
                    if dec:
                        room['action']['bridge_declared'] = True
                        add_log(room, f"🌉 {room['players'][pi]['name']} объявляет БРИДЖ!")
                        resolve_action(room, pi)
                    else:
                        add_log(room, f"{room['players'][pi]['name']} отказывается от Бриджа и играет дальше.")
                        schedule_bot(room)
                    return self.send_json({'ok': True})

                if path == '/api/next_round':
                    if pi != 0:
                        return self.send_json({'error': 'Только хозяин'}, 403)
                    if not room['round_over'] or room['match_over']:
                        return self.send_json({'error': 'Сейчас нельзя начать следующий раунд'}, 409)
                    room['round'] += 1
                    room['dealer'] = next_seat(room, room['dealer'])
                    start_round(room)
                    return self.send_json({'ok': True})

                if path == '/api/new_match':
                    if pi != 0:
                        return self.send_json({'error': 'Только хозяин'}, 403)
                    start_match(room)
                    return self.send_json({'ok': True})

                if path == '/api/chat':
                    msg = str(body.get('msg', '')).strip()[:160]
                    if not msg:
                        return self.send_json({'error': 'Пустое сообщение'}, 400)
                    room.setdefault('chat', []).append({'t': int(now()), 'seat': pi, 'name': room['players'][pi]['name'], 'msg': msg})
                    room['chat'] = room['chat'][-100:]
                    room['version'] += 1
                    return self.send_json({'ok': True})

                if path == '/api/reaction':
                    key = str(body.get('key', '')).strip()[:40]
                    allowed = {'heat','laugh','rigged','mindblown','joker','boss','sneaky','combo','anime-laugh','anime-rage','anime-shock','anime-cry','anime-smug','anime-love','anime-applause','anime-sleep'}
                    if key not in allowed:
                        return self.send_json({'error': 'Неизвестная эмоция'}, 400)
                    room['reaction_seq'] = int(room.get('reaction_seq', 0)) + 1
                    room.setdefault('reactions', []).append({'id': room['reaction_seq'], 't': int(now()), 'seat': pi, 'key': key})
                    room['reactions'] = room['reactions'][-40:]
                    room['version'] += 1
                    return self.send_json({'ok': True})

                if path == '/api/close_room':
                    if pi != 0:
                        return self.send_json({'error': 'Только хозяин может закрыть комнату'}, 403)
                    del ROOMS[code]
                    return self.send_json({'ok': True})

                return self.send_json({'error': 'Неизвестная команда'}, 404)
            except Exception as e:
                print('API ERROR', path, repr(e), flush=True)
                return self.send_json({'error': 'Ошибка сервера: ' + str(e)}, 500)

if __name__ == '__main__':
    os.chdir(PUBLIC)
    print('\n=== БРИДЖ РУБИКА — FULL ONLINE SERVER ===', flush=True)
    print(f'Listening on {HOST}:{PORT}', flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
