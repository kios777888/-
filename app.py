"""
شكون لمافيا - Enhanced Backend with Chat & Timing
"""
from flask import Flask, render_template, request, jsonify, session, make_response, send_from_directory
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, emit, join_room, leave_room
from werkzeug.security import generate_password_hash, check_password_hash
import jwt, os, json, random, string, uuid, logging, time
from datetime import datetime, timedelta
from functools import wraps

# ============================================================================
# SETUP
# ============================================================================

app = Flask(__name__, template_folder='templates', static_folder='templates')
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-2024-change-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///mafia.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['JSON_AS_ASCII'] = False
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('FLASK_ENV') == 'production'
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

db = SQLAlchemy(app)
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode='threading',
    ping_timeout=60,
    ping_interval=25,
    logger=False,
    engineio_logger=False
)
CORS(app, resources={r"/*": {"origins": "*", "methods": ["GET", "POST", "OPTIONS"]}})

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

JWT_SECRET = app.config['SECRET_KEY']
JWT_EXPIRATION_HOURS = 24

# ============================================================================
# DATABASE MODELS
# ============================================================================

class User(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email = db.Column(db.String(120), unique=True, nullable=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=True)
    avatar = db.Column(db.String(255), default='default')
    wins = db.Column(db.Integer, default=0)
    losses = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password, method='pbkdf2:sha256')
    
    def check_password(self, password):
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)
    
    def to_dict(self):
        return {
            'id': self.id,
            'username': self.username,
            'email': self.email,
            'avatar': self.avatar,
            'wins': self.wins,
            'losses': self.losses,
            'winRate': round(self.wins / (self.wins + self.losses) * 100, 1) if (self.wins + self.losses) > 0 else 0
        }

class GameRoom(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = db.Column(db.String(120), nullable=False)
    host_id = db.Column(db.String(36), db.ForeignKey('user.id'), nullable=True)
    is_public = db.Column(db.Boolean, default=True)
    max_players = db.Column(db.Integer, default=8)
    mafia_count = db.Column(db.Integer, default=2)
    detective_count = db.Column(db.Integer, default=1)
    doctor_count = db.Column(db.Integer, default=1)
    status = db.Column(db.String(20), default='waiting')
    players_data = db.Column(db.Text, default='{}')
    game_state = db.Column(db.Text, default='{}')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    started_at = db.Column(db.DateTime)
    ended_at = db.Column(db.DateTime)
    
    def to_dict(self):
        try:
            players_count = len(json.loads(self.players_data or '{}'))
        except (json.JSONDecodeError, TypeError):
            players_count = 0
        return {
            'id': self.id,
            'name': self.name,
            'host_id': self.host_id,
            'is_public': self.is_public,
            'max_players': self.max_players,
            'status': self.status,
            'player_count': players_count,
            'mafia_count': self.mafia_count,
            'detective_count': self.detective_count,
            'doctor_count': self.doctor_count
        }

# ============================================================================
# GAME CONSTANTS
# ============================================================================

ROLES = {
    'mafia': {'ar': 'مافيا', 'en': 'Mafia', 'icon': '🔫', 'color': '#c41e3a'},
    'detective': {'ar': 'محقق', 'en': 'Detective', 'icon': '🔍', 'color': '#4287f5'},
    'doctor': {'ar': 'طبيب', 'en': 'Doctor', 'icon': '🏥', 'color': '#2ecc71'},
    'villager': {'ar': 'مواطن', 'en': 'Villager', 'icon': '👥', 'color': '#95a5a6'}
}

PHASE_NIGHT = 'night'
PHASE_DAY = 'day'
PHASE_ENDED = 'ended'

PHASE_DURATIONS = {
    'night': 60,
    'day': 90
}

# ============================================================================
# GAME LOGIC
# ============================================================================

def distribute_roles(player_count, mafia_count, detective_count, doctor_count):
    if player_count < 4:
        return None
    
    roles = []
    mafia_actual = min(mafia_count, max(1, player_count - 1))
    roles.extend(['mafia'] * mafia_actual)
    
    detective_actual = min(detective_count, player_count - len(roles))
    roles.extend(['detective'] * detective_actual)
    
    doctor_actual = min(doctor_count, player_count - len(roles))
    roles.extend(['doctor'] * doctor_actual)
    
    villager_actual = player_count - len(roles)
    roles.extend(['villager'] * villager_actual)
    
    assert len(roles) == player_count
    random.shuffle(roles)
    return roles

def check_win_condition(players):
    alive_players = {sid: p for sid, p in players.items() if p.get('alive', True)}
    if not alive_players:
        return None
    
    mafia_alive = sum(1 for p in alive_players.values() if p.get('role') == 'mafia')
    villagers_alive = sum(1 for p in alive_players.values() if p.get('role') != 'mafia')
    
    if mafia_alive == 0:
        return 'villagers'
    elif mafia_alive >= villagers_alive:
        return 'mafia'
    return None

def get_or_create_room(room_id):
    if room_id not in active_rooms:
        active_rooms[room_id] = {
            'players': {},
            'phase': PHASE_NIGHT,
            'round': 1,
            'mafia_target': None,
            'doctor_target': None,
            'detective_target': None,
            'day_votes': {},
            'eliminated': None,
            'killed': None,
            'night_actions_ready': {},
            'phase_start_time': time.time(),
            'phase_timer': None,
            'mafia_chat': [],
            'public_chat': []
        }
    return active_rooms[room_id]

# ============================================================================
# GLOBAL STATE
# ============================================================================

active_rooms = {}
player_sockets = {}

# ============================================================================
# STATIC FILE ROUTES
# ============================================================================

@app.route('/print.html')
def serve_print():
    return send_from_directory('templates', 'print.html')

@app.route('/img/<path:filename>')
def serve_image(filename):
    try:
        return send_from_directory('img', filename)
    except:
        return "Image not found", 404

@app.route('/music/<path:filename>')
def serve_music(filename):
    try:
        return send_from_directory('music', filename)
    except:
        return "Music not found", 404

@app.route('/fonts/<path:filename>')
def serve_fonts(filename):
    try:
        return send_from_directory('fonts', filename)
    except:
        return "Font not found", 404

# ============================================================================
# REST API ROUTES
# ============================================================================

def create_token(user_id):
    payload = {
        'user_id': user_id,
        'exp': datetime.utcnow() + timedelta(hours=JWT_EXPIRATION_HOURS)
    }
    return jwt.encode(payload, JWT_SECRET, algorithm='HS256')

@app.route('/api/auth/register', methods=['POST', 'OPTIONS'])
def register():
    if request.method == 'OPTIONS':
        return '', 204
    
    data = request.json or {}
    username = data.get('username', '').strip()
    email = data.get('email', '').strip().lower()
    password = data.get('password', '')
    
    if not username or len(username) < 3:
        return jsonify({'error': 'Username must be at least 3 characters'}), 400
    if not email or '@' not in email:
        return jsonify({'error': 'Invalid email'}), 400
    if not password or len(password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400
    
    if User.query.filter_by(username=username).first():
        return jsonify({'error': 'Username already taken'}), 409
    if User.query.filter_by(email=email).first():
        return jsonify({'error': 'Email already registered'}), 409
    
    user = User(email=email, username=username)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    
    token = create_token(user.id)
    response = make_response(jsonify({
        'token': token,
        'user': user.to_dict()
    }), 201)
    
    response.set_cookie('auth_token', token, httponly=True, secure=False, samesite='Lax', max_age=86400*7)
    logger.info(f"✓ User registered: {username}")
    return response

@app.route('/api/auth/login', methods=['POST', 'OPTIONS'])
def login():
    if request.method == 'OPTIONS':
        return '', 204
    
    data = request.json or {}
    email = data.get('email', '').strip().lower()
    password = data.get('password', '')
    
    if not email or not password:
        return jsonify({'error': 'Email and password required'}), 400
    
    user = User.query.filter_by(email=email).first()
    if not user or not user.check_password(password):
        return jsonify({'error': 'Invalid credentials'}), 401
    
    token = create_token(user.id)
    response = make_response(jsonify({
        'token': token,
        'user': user.to_dict()
    }), 200)
    
    response.set_cookie('auth_token', token, httponly=True, secure=False, samesite='Lax', max_age=86400*7)
    logger.info(f"✓ User logged in: {email}")
    return response

@app.route('/api/auth/logout', methods=['POST', 'OPTIONS'])
def logout():
    if request.method == 'OPTIONS':
        return '', 204
    
    response = make_response(jsonify({'message': 'Logged out'}), 200)
    response.set_cookie('auth_token', '', expires=0)
    return response

@app.route('/api/auth/guest', methods=['POST', 'OPTIONS'])
def guest_login():
    if request.method == 'OPTIONS':
        return '', 204
    
    try:
        username = f"Guest_{uuid.uuid4().hex[:6].upper()}"
        temp_email = f"guest_{uuid.uuid4().hex[:8]}@temp.local"
        
        user = User(email=temp_email, username=username)
        db.session.add(user)
        db.session.commit()
        
        token = create_token(user.id)
        response = make_response(jsonify({
            'token': token,
            'user': user.to_dict(),
            'is_guest': True
        }), 201)
        
        response.set_cookie('auth_token', token, httponly=True, secure=False, samesite='Lax', max_age=86400*7)
        logger.info(f"✓ Guest created: {username}")
        return response
    except Exception as e:
        logger.error(f"❌ Guest login error: {str(e)}")
        db.session.rollback()
        return jsonify({'error': f'Guest login failed: {str(e)}'}), 500

@app.route('/api/user/<user_id>/stats', methods=['GET', 'OPTIONS'])
def get_user_stats(user_id):
    if request.method == 'OPTIONS':
        return '', 204
    
    user = User.query.get(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    return jsonify(user.to_dict()), 200

@app.route('/api/leaderboard', methods=['GET', 'OPTIONS'])
def get_leaderboard():
    if request.method == 'OPTIONS':
        return '', 204
    
    limit = request.args.get('limit', 10, type=int)
    users = User.query.order_by(User.wins.desc()).limit(limit).all()
    return jsonify([{**u.to_dict(), 'rank': i+1} for i, u in enumerate(users)]), 200

@app.route('/api/rooms', methods=['POST', 'OPTIONS', 'GET'])
def rooms_handler():
    if request.method == 'OPTIONS':
        return '', 204
    
    if request.method == 'POST':
        try:
            data = request.json or {}
            host_id = data.get('host_id')
            
            if not host_id:
                temp_user = User(username=f"Host_{uuid.uuid4().hex[:6].upper()}")
                db.session.add(temp_user)
                db.session.commit()
                host_id = temp_user.id
            
            room = GameRoom(
                name=data.get('name', 'Game Room'),
                host_id=host_id,
                is_public=data.get('is_public', True),
                max_players=int(data.get('max_players', 8)),
                mafia_count=int(data.get('mafia_count', 2)),
                detective_count=int(data.get('detective_count', 1)),
                doctor_count=int(data.get('doctor_count', 1))
            )
            db.session.add(room)
            db.session.commit()
            
            logger.info(f"✓ Room created: {room.id} - {room.name}")
            return jsonify(room.to_dict()), 201
        except Exception as e:
            logger.error(f"Error creating room: {e}")
            db.session.rollback()
            return jsonify({'error': f'Failed to create room: {str(e)}'}), 500
    
    else:  # GET
        rooms_query = GameRoom.query.filter_by(is_public=True, status='waiting').limit(50)
        rooms_list = []
        for room in rooms_query:
            try:
                players = json.loads(room.players_data or '{}')
            except (json.JSONDecodeError, TypeError):
                players = {}
            if players:
                rooms_list.append(room)
        return jsonify([room.to_dict() for room in rooms_list]), 200

@app.route('/api/rooms/<room_id>', methods=['GET', 'OPTIONS'])
def get_room(room_id):
    if request.method == 'OPTIONS':
        return '', 204
    
    room = GameRoom.query.get(room_id)
    if not room:
        return jsonify({'error': 'Room not found'}), 404
    
    room_data = room.to_dict()
    try:
        room_data['players'] = json.loads(room.players_data or '{}')
        room_data['gameState'] = json.loads(room.game_state or '{}')
    except (json.JSONDecodeError, TypeError):
        room_data['players'] = {}
        room_data['gameState'] = {}
    
    return jsonify(room_data), 200

@app.route('/print', methods=['GET'])
def print_sheet():
    return render_template('print.html')

@app.route('/', methods=['GET'])
def index():
    return render_template('index.html')

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'timestamp': datetime.utcnow().isoformat()}), 200

# ============================================================================
# SOCKET.IO EVENTS
# ============================================================================

@socketio.on('connect')
def handle_connect():
    sid = request.sid
    logger.info(f"✓ Client connected: {sid}")
    emit('connection_response', {'data': 'Connected', 'sid': sid})

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    logger.info(f"✗ Client disconnected: {sid}")
    
    for room_id, room in list(active_rooms.items()):
        if sid in room.get('players', {}):
            del room['players'][sid]
            
            db_room = GameRoom.query.get(room_id)
            if db_room:
                try:
                    db_room.players_data = json.dumps(room['players'])
                    if not room['players']:
                        db.session.delete(db_room)
                        active_rooms.pop(room_id, None)
                    db.session.commit()
                except Exception as e:
                    logger.error(f"DB sync error on disconnect: {e}")
                    db.session.rollback()
            
            socketio.emit('player_left', {
                'player_count': len(room.get('players', {})),
                'players': room.get('players', {})
            }, room=room_id)

@socketio.on('join_room')
def on_join_room(data):
    room_id = data.get('room_id')
    nickname = data.get('nickname', f"Player_{uuid.uuid4().hex[:4].upper()}")
    sid = request.sid
    
    db_room = GameRoom.query.get(room_id)
    if not db_room:
        emit('error', {'message': 'Room not found'})
        return
    
    room = get_or_create_room(room_id)
    
    room['players'][sid] = {
        'sid': sid,
        'nickname': nickname,
        'role': None,
        'alive': True,
        'voted': False,
        'vote_for': None
    }
    
    try:
        db_room.players_data = json.dumps(room['players'])
        db.session.commit()
    except Exception as e:
        logger.error(f"DB sync error on join: {e}")
        db.session.rollback()
    
    join_room(room_id)
    logger.info(f"→ {nickname} joined {room_id}. Total: {len(room['players'])}")
    
    emit('player_joined', {
        'player': room['players'][sid],
        'players': room['players'],
        'total': len(room['players'])
    }, room=room_id)

@socketio.on('leave_room')
def on_leave_room(data):
    room_id = data.get('room_id')
    sid = request.sid
    
    if room_id in active_rooms and sid in active_rooms[room_id]['players']:
        del active_rooms[room_id]['players'][sid]
        
        db_room = GameRoom.query.get(room_id)
        if db_room:
            try:
                db_room.players_data = json.dumps(active_rooms[room_id]['players'])
                if not active_rooms[room_id]['players']:
                    db.session.delete(db_room)
                    active_rooms.pop(room_id, None)
                db.session.commit()
            except Exception as e:
                logger.error(f"DB sync error on leave: {e}")
                db.session.rollback()
        
        leave_room(room_id)
        emit('player_left', {
            'player_count': len(active_rooms.get(room_id, {}).get('players', {}))
        }, room=room_id)

@socketio.on('chat_message')
def on_chat(data):
    room_id = data.get('room_id')
    message = data.get('message', '').strip()
    sid = request.sid
    
    if room_id not in active_rooms or sid not in active_rooms[room_id]['players']:
        return
    
    room = active_rooms[room_id]
    player = room['players'][sid]
    
    if room['phase'] != PHASE_DAY:
        emit('error', {'message': 'Public chat only available during day phase'})
        return
    
    chat_message = {
        'from': player['nickname'],
        'text': message,
        'ts': datetime.utcnow().isoformat(),
        'type': 'public'
    }
    
    room['public_chat'].append(chat_message)
    emit('chat_message', chat_message, room=room_id)

@socketio.on('start_game')
def on_start_game(data):
    room_id = data.get('room_id')
    sid = request.sid
    
    if room_id not in active_rooms:
        emit('error', {'message': 'Room not found'})
        return
    
    room = active_rooms[room_id]
    db_room = GameRoom.query.get(room_id)
    if not db_room:
        emit('error', {'message': 'Room not found in database'})
        return
    
    players_list = list(room['players'].values())
    player_count = len(players_list)
    
    if player_count < 4:
        emit('error', {'message': f'Need 4+ players, got {player_count}'}, room=room_id)
        return
    
    roles = distribute_roles(player_count, db_room.mafia_count, db_room.detective_count, db_room.doctor_count)
    
    if not roles:
        emit('error', {'message': 'Invalid role configuration'}, room=room_id)
        return
    
    sids = list(room['players'].keys())
    for i, player_sid in enumerate(sids):
        room['players'][player_sid]['role'] = roles[i]
    
    room['phase'] = PHASE_NIGHT
    room['round'] = 1
    room['phase_start_time'] = time.time()
    db_room.status = 'playing'
    db_room.started_at = datetime.utcnow()
    
    try:
        db_room.game_state = json.dumps({'phase': PHASE_NIGHT, 'round': 1})
        db.session.commit()
    except Exception as e:
        logger.error(f"DB error on game start: {e}")
        db.session.rollback()
    
    emit('game_started', {
        'phase': PHASE_NIGHT,
        'round': 1,
        'message': '🌙 Night phase. Mafia, choose target!',
        'players': room['players']
    }, room=room_id)
    
    logger.info(f"🎮 Game started in {room_id} with {player_count} players")

@socketio.on('night_action')
def on_night_action(data):
    room_id = data.get('room_id')
    action = data.get('action')
    target_sid = data.get('target_sid')
    sid = request.sid
    
    if room_id not in active_rooms:
        emit('error', {'message': 'Room not found'})
        return
    
    room = active_rooms[room_id]
    
    if sid not in room['players'] or target_sid not in room['players']:
        emit('error', {'message': 'Invalid action'})
        return
    
    player = room['players'][sid]
    target_name = room['players'][target_sid]['nickname']
    
    if player['role'] == 'mafia' and action == 'kill':
        room['mafia_target'] = target_sid
        room['night_actions_ready'][sid] = True
        emit('action_feedback', {'message': f"🔫 Target: {target_name}"}, room=sid)
    elif player['role'] == 'doctor' and action == 'heal':
        room['doctor_target'] = target_sid
        room['night_actions_ready'][sid] = True
        emit('action_feedback', {'message': f"🏥 Healing: {target_name}"}, room=sid)
    elif player['role'] == 'detective' and action == 'investigate':
        room['detective_target'] = target_sid
        room['night_actions_ready'][sid] = True
        target_role = room['players'][target_sid]['role']
        is_mafia = target_role == 'mafia'
        emit('investigation_result', {'target': target_name, 'is_mafia': is_mafia}, room=sid)

@socketio.on('day_vote')
def on_day_vote(data):
    room_id = data.get('room_id')
    vote_for_sid = data.get('vote_for_sid')
    sid = request.sid
    
    if room_id not in active_rooms:
        emit('error', {'message': 'Room not found'})
        return
    
    room = active_rooms[room_id]
    
    if sid not in room['players'] or vote_for_sid not in room['players']:
        emit('error', {'message': 'Invalid vote'})
        return
    
    room['day_votes'][sid] = vote_for_sid
    emit('vote_cast', {
        'voter': room['players'][sid]['nickname'],
        'voted': room['players'][vote_for_sid]['nickname']
    }, room=room_id)

# ============================================================================
# MAIN
# ============================================================================

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        logger.info("✓ Database initialized")
    
    logger.info("🎭 شكون لمافيا server starting...")
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
