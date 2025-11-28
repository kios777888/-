"""
شكون لمافيا - Enhanced Mafia Game Backend
Fixed version with proper phase transitions and room cleanup
"""
from flask import Flask, render_template, request, jsonify, send_from_directory
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, emit, join_room, leave_room
from werkzeug.security import generate_password_hash, check_password_hash
import jwt
import os
import json
import random
import uuid
import logging
import time
from datetime import datetime, timedelta
from threading import Timer

# ============================================================================
# CONFIGURATION
# ============================================================================

app = Flask(__name__, template_folder='templates', static_folder='templates')

app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-key-2024')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///mafia.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['JSON_AS_ASCII'] = False
app.config['SESSION_COOKIE_SECURE'] = False
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

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
CORS(app, resources={r"/*": {"origins": "*", "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"]}})

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
        total = self.wins + self.losses
        win_rate = round(self.wins / total * 100, 1) if total > 0 else 0
        return {
            'id': self.id,
            'username': self.username,
            'email': self.email,
            'avatar': self.avatar,
            'wins': self.wins,
            'losses': self.losses,
            'winRate': win_rate
        }


class GameRoom(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = db.Column(db.String(120), nullable=False)
    host_id = db.Column(db.String(36), nullable=True)
    is_public = db.Column(db.Boolean, default=True)
    max_players = db.Column(db.Integer, default=8)
    mafia_count = db.Column(db.Integer, default=2)
    detective_count = db.Column(db.Integer, default=1)
    doctor_count = db.Column(db.Integer, default=1)
    status = db.Column(db.String(20), default='waiting')
    players_data = db.Column(db.Text, default='{}')
    game_state = db.Column(db.Text, default='{}')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    started_at = db.Column(db.DateTime, nullable=True)
    ended_at = db.Column(db.DateTime, nullable=True)
    
    def to_dict(self):
        try:
            players = json.loads(self.players_data or '{}')
            player_count = len(players)
        except:
            player_count = 0
        
        return {
            'id': self.id,
            'name': self.name,
            'host_id': self.host_id,
            'is_public': self.is_public,
            'max_players': self.max_players,
            'status': self.status,
            'player_count': player_count,
            'mafia_count': self.mafia_count,
            'detective_count': self.detective_count,
            'doctor_count': self.doctor_count
        }


# ============================================================================
# GAME STATE
# ============================================================================

active_rooms = {}
ROLES = {
    'mafia': {'ar': 'مافيا', 'en': 'Mafia', 'icon': '🔫', 'color': '#dc2626'},
    'detective': {'ar': 'محقق', 'en': 'Detective', 'icon': '🔍', 'color': '#3b82f6'},
    'doctor': {'ar': 'طبيب', 'en': 'Doctor', 'icon': '🏥', 'color': '#10b981'},
    'villager': {'ar': 'مواطن', 'en': 'Villager', 'icon': '👥', 'color': '#6b7280'}
}

NIGHT_DURATION = 30  # seconds
DAY_DURATION = 45    # seconds


def get_or_create_room(room_id):
    if room_id not in active_rooms:
        active_rooms[room_id] = {
            'players': {},
            'phase': 'waiting',
            'round': 0,
            'mafia_targets': {},  # FIXED: Track all mafia targets
            'doctor_target': None,
            'detective_target': None,
            'day_votes': {},
            'phase_timer': None,
            'created_at': time.time()
        }
    return active_rooms[room_id]


def cleanup_empty_room(room_id):
    """Delete room if no players"""
    room = active_rooms.get(room_id)
    if not room or len(room.get('players', {})) == 0:
        # Cancel any pending timers
        if room and room.get('phase_timer'):
            room['phase_timer'].cancel()
        
        # Delete from database
        try:
            db_room = GameRoom.query.get(room_id)
            if db_room:
                db.session.delete(db_room)
                db.session.commit()
        except:
            db.session.rollback()
        
        # Delete from active rooms
        if room_id in active_rooms:
            del active_rooms[room_id]
        
        logger.info(f"🗑️ Deleted empty room: {room_id}")


# ============================================================================
# PHASE TRANSITION FUNCTIONS
# ============================================================================

def transition_to_day(room_id):
    """Execute night actions and move to day"""
    room = active_rooms.get(room_id)
    if not room:
        return
    
    db_room = GameRoom.query.get(room_id)
    if not db_room:
        return
    
    players = room['players']
    
    # FIXED: Resolve mafia target (random from all submissions)
    mafia_targets = list(room['mafia_targets'].values())
    killed_sid = None
    
    if mafia_targets:
        # If multiple targets, pick random one (or let them all be killed?)
        # For now: pick the one with most votes, or random if tied
        from collections import Counter
        vote_counts = Counter(mafia_targets)
        killed_sid = vote_counts.most_common(1)[0][0]
    
    # Check if doctor healed the target
    doctor_saved = room['doctor_target'] == killed_sid
    if doctor_saved and killed_sid:
        killed_sid = None
    
    # Mark killed player as dead
    if killed_sid and killed_sid in players:
        players[killed_sid]['alive'] = False
        message = f"☀️ {players[killed_sid]['nickname']} was killed by the Mafia!"
    else:
        message = "☀️ The Mafia couldn't kill anyone last night (doctor saved them!)"
    
    # Check win conditions
    winner = check_win_condition(room)
    
    if winner:
        end_game(room_id, winner, room)
    else:
        # Move to day phase
        room['phase'] = 'day'
        room['round'] += 1
        room['day_votes'] = {}
        
        db_room.game_state = json.dumps({
            'phase': 'day',
            'round': room['round'],
            'message': message,
            'killed_sid': killed_sid
        })
        db.session.commit()
        
        socketio.emit('phase_change', {
            'phase': 'day',
            'round': room['round'],
            'message': message,
            'killed': killed_sid,
            'players': players
        }, room=room_id)
        
        # Schedule day->night transition
        schedule_phase_transition(room_id, 'night', DAY_DURATION)


def transition_to_night(room_id):
    """Execute day voting and move to night"""
    room = active_rooms.get(room_id)
    if not room:
        return
    
    db_room = GameRoom.query.get(room_id)
    if not db_room:
        return
    
    players = room['players']
    
    # Get votes and execute
    votes = room['day_votes']
    if votes:
        from collections import Counter
        vote_counts = Counter(votes.values())
        executed_sid = vote_counts.most_common(1)[0][0]
        
        if executed_sid in players:
            players[executed_sid]['alive'] = False
            message = f"⚖️ {players[executed_sid]['nickname']} was voted out!"
        else:
            message = "⚖️ Vote completed"
    else:
        message = "⚖️ No votes cast, no one eliminated"
    
    # Check win conditions
    winner = check_win_condition(room)
    
    if winner:
        end_game(room_id, winner, room)
    else:
        # Move to night phase
        room['phase'] = 'night'
        room['mafia_targets'] = {}
        room['doctor_target'] = None
        room['detective_target'] = None
        
        db_room.game_state = json.dumps({
            'phase': 'night',
            'round': room['round'],
            'message': '🌙 Night phase started'
        })
        db.session.commit()
        
        socketio.emit('phase_change', {
            'phase': 'night',
            'round': room['round'],
            'message': '🌙 Night phase started',
            'players': players
        }, room=room_id)
        
        # Schedule night->day transition
        schedule_phase_transition(room_id, 'day', NIGHT_DURATION)


def schedule_phase_transition(room_id, next_phase, delay):
    """Schedule the next phase transition"""
    room = active_rooms.get(room_id)
    if not room:
        return
    
    # Cancel previous timer
    if room.get('phase_timer'):
        room['phase_timer'].cancel()
    
    def do_transition():
        try:
            if next_phase == 'day':
                transition_to_day(room_id)
            elif next_phase == 'night':
                transition_to_night(room_id)
        except Exception as e:
            logger.error(f"Phase transition error: {e}")
    
    timer = Timer(delay, do_transition)
    timer.daemon = True
    timer.start()
    
    room['phase_timer'] = timer
    logger.info(f"⏱️ Scheduled {next_phase} phase in {delay}s for room {room_id}")


def check_win_condition(room):
    """Check if game is over, return winner or None"""
    players = room['players']
    alive_players = [p for p in players.values() if p['alive']]
    alive_mafia = [p for p in alive_players if p.get('role') == 'mafia']
    
    if not alive_mafia:
        return 'town'
    if len(alive_mafia) >= len(alive_players) / 2:
        return 'mafia'
    
    return None


def end_game(room_id, winner, room):
    """End the game"""
    room['phase'] = 'ended'
    
    db_room = GameRoom.query.get(room_id)
    if db_room:
        db_room.status = 'ended'
        db_room.ended_at = datetime.utcnow()
        db.session.commit()
    
    message = f"🎉 {'Mafia' if winner == 'mafia' else 'Town'} wins!"
    
    socketio.emit('game_ended', {
        'phase': 'ended',
        'winner': winner,
        'message': message,
        'players': room['players']
    }, room=room_id)
    
    logger.info(f"🏁 Game ended in {room_id}, winner: {winner}")


# ============================================================================
# STATIC FILE ROUTES
# ============================================================================

@app.route('/img/<path:filename>')
def serve_image(filename):
    try:
        return send_from_directory('img', filename)
    except:
        return "Not found", 404


@app.route('/music/<path:filename>')
def serve_music(filename):
    try:
        return send_from_directory('music', filename)
    except:
        return "Not found", 404


@app.route('/fonts/<path:filename>')
def serve_fonts(filename):
    try:
        return send_from_directory('fonts', filename)
    except:
        return "Not found", 404


# ============================================================================
# MAIN ROUTES
# ============================================================================

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'ok',
        'timestamp': datetime.utcnow().isoformat(),
        'service': 'شكون لمافيا API'
    }), 200


# ============================================================================
# AUTH API
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
    
    try:
        data = request.get_json() or {}
        username = (data.get('username') or '').strip()
        email = (data.get('email') or '').strip().lower()
        password = data.get('password') or ''
        
        if not username or len(username) < 3:
            return jsonify({'error': 'Username min 3 chars'}), 400
        if not email or '@' not in email:
            return jsonify({'error': 'Invalid email'}), 400
        if not password or len(password) < 6:
            return jsonify({'error': 'Password min 6 chars'}), 400
        
        if User.query.filter_by(username=username).first():
            return jsonify({'error': 'Username taken'}), 409
        if User.query.filter_by(email=email).first():
            return jsonify({'error': 'Email taken'}), 409
        
        user = User(username=username, email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        
        token = create_token(user.id)
        response = jsonify({'token': token, 'user': user.to_dict()})
        response.status_code = 201
        logger.info(f"✓ User registered: {username}")
        return response
    except Exception as e:
        logger.error(f"Register error: {e}")
        db.session.rollback()
        return jsonify({'error': str(e)}), 500


@app.route('/api/auth/login', methods=['POST', 'OPTIONS'])
def login():
    if request.method == 'OPTIONS':
        return '', 204
    
    try:
        data = request.get_json() or {}
        email = (data.get('email') or '').strip().lower()
        password = data.get('password') or ''
        
        if not email or not password:
            return jsonify({'error': 'Email and password required'}), 400
        
        user = User.query.filter_by(email=email).first()
        if not user or not user.check_password(password):
            return jsonify({'error': 'Invalid credentials'}), 401
        
        token = create_token(user.id)
        logger.info(f"✓ User logged in: {email}")
        return jsonify({'token': token, 'user': user.to_dict()}), 200
    except Exception as e:
        logger.error(f"Login error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/auth/guest', methods=['POST', 'OPTIONS'])
def guest_login():
    if request.method == 'OPTIONS':
        return '', 204
    
    try:
        username = f"Guest_{uuid.uuid4().hex[:6].upper()}"
        temp_email = f"guest_{uuid.uuid4().hex[:8]}@temp.local"
        
        user = User(username=username, email=temp_email)
        db.session.add(user)
        db.session.commit()
        
        token = create_token(user.id)
        logger.info(f"✓ Guest created: {username}")
        
        return jsonify({
            'token': token,
            'user': user.to_dict(),
            'is_guest': True
        }), 201
    except Exception as e:
        logger.error(f"Guest login error: {e}")
        db.session.rollback()
        return jsonify({'error': f'Guest login failed: {str(e)}'}), 500


# ============================================================================
# ROOMS API
# ============================================================================

@app.route('/api/rooms', methods=['GET', 'POST', 'OPTIONS'])
def rooms_handler():
    if request.method == 'OPTIONS':
        return '', 204
    
    if request.method == 'GET':
        try:
            rooms = GameRoom.query.filter_by(is_public=True, status='waiting').limit(50).all()
            return jsonify([r.to_dict() for r in rooms]), 200
        except Exception as e:
            logger.error(f"Get rooms error: {e}")
            return jsonify({'error': str(e)}), 500
    
    else:  # POST
        try:
            data = request.get_json() or {}
            host_id = data.get('host_id')
            
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
            
            get_or_create_room(room.id)
            logger.info(f"✓ Room created: {room.name}")
            return jsonify(room.to_dict()), 201
        except Exception as e:
            logger.error(f"Create room error: {e}")
            db.session.rollback()
            return jsonify({'error': str(e)}), 500


@app.route('/api/rooms/<room_id>', methods=['GET', 'OPTIONS'])
def get_room(room_id):
    if request.method == 'OPTIONS':
        return '', 204
    
    try:
        room = GameRoom.query.get(room_id)
        if not room:
            return jsonify({'error': 'Room not found'}), 404
        
        data = room.to_dict()
        data['players'] = json.loads(room.players_data or '{}')
        data['gameState'] = json.loads(room.game_state or '{}')
        return jsonify(data), 200
    except Exception as e:
        logger.error(f"Get room error: {e}")
        return jsonify({'error': str(e)}), 500


# ============================================================================
# SOCKET.IO EVENTS
# ============================================================================

@socketio.on('connect')
def handle_connect():
    logger.info(f"✓ Socket connected: {request.sid}")
    emit('connection_response', {'data': 'Connected', 'sid': request.sid})


@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    logger.info(f"✗ Socket disconnected: {sid}")
    
    for room_id in list(active_rooms.keys()):
        room = active_rooms[room_id]
        if sid in room.get('players', {}):
            del room['players'][sid]
            
            db_room = GameRoom.query.get(room_id)
            if db_room:
                try:
                    db_room.players_data = json.dumps(room['players'])
                    db.session.commit()
                except:
                    db.session.rollback()
            
            emit('player_left', {
                'player_count': len(room.get('players', {}))
            }, room=room_id)
            
            # FIXED: Cleanup empty rooms
            cleanup_empty_room(room_id)


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
        'alive': True
    }
    
    try:
        db_room.players_data = json.dumps(room['players'])
        db.session.commit()
    except:
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
                db.session.commit()
            except:
                db.session.rollback()
        
        leave_room(room_id)
        emit('player_left', {'player_count': 0}, room=room_id)
        
        # FIXED: Cleanup empty rooms
        cleanup_empty_room(room_id)


@socketio.on('start_game')
def on_start_game(data):
    room_id = data.get('room_id')
    
    if room_id not in active_rooms:
        emit('error', {'message': 'Room not found'})
        return
    
    room = active_rooms[room_id]
    db_room = GameRoom.query.get(room_id)
    
    if not db_room:
        emit('error', {'message': 'DB room not found'})
        return
    
    players_list = list(room['players'].values())
    if len(players_list) < 3:
        emit('error', {'message': f'Need 3+ players, have {len(players_list)}'}, room=room_id)
        return
    
    roles = ['mafia', 'mafia', 'detective', 'doctor'] + ['villager'] * (len(players_list) - 4)
    random.shuffle(roles)
    
    sids = list(room['players'].keys())
    for i, player_sid in enumerate(sids):
        room['players'][player_sid]['role'] = roles[i]
    
    room['phase'] = 'night'
    room['round'] = 1
    
    db_room.status = 'playing'
    db_room.started_at = datetime.utcnow()
    
    try:
        db_room.game_state = json.dumps({'phase': 'night', 'round': 1})
        db.session.commit()
    except:
        db.session.rollback()
    
    for player_sid, player in room['players'].items():
        role_info = ROLES.get(player['role'], {})
        socketio.emit('role_assigned', {
            'role': player['role'],
            'role_name': role_info.get('en', 'Unknown'),
            'role_ar': role_info.get('ar', 'غير معروف'),
            'icon': role_info.get('icon', ''),
            'color': role_info.get('color', '#000000')
        }, room=player_sid)
    
    emit('game_started', {
        'phase': 'night',
        'round': 1,
        'message': '🌙 Night phase started',
        'players': room['players']
    }, room=room_id)
    
    logger.info(f"🎮 Game started in {room_id}")
    
    # FIXED: Schedule first day transition
    schedule_phase_transition(room_id, 'day', NIGHT_DURATION)


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
    target = room['players'][target_sid]
    
    if action == 'kill' and player['role'] == 'mafia':
        # FIXED: Store mafia target (each mafia member submits)
        room['mafia_targets'][sid] = target_sid
        emit('action_feedback', {'message': f"🔫 Target: {target['nickname']}"}, room=sid)
        logger.info(f"🔫 Mafia {sid} targeting {target_sid}")
    
    elif action == 'heal' and player['role'] == 'doctor':
        room['doctor_target'] = target_sid
        emit('action_feedback', {'message': f"🏥 Healing: {target['nickname']}"}, room=sid)
        logger.info(f"🏥 Doctor {sid} healing {target_sid}")
    
    elif action == 'investigate' and player['role'] == 'detective':
        room['detective_target'] = target_sid
        is_mafia = target['role'] == 'mafia'
        emit('investigation_result', {
            'target': target['nickname'],
            'is_mafia': is_mafia
        }, room=sid)
        logger.info(f"🔍 Detective {sid} investigated {target_sid}: {'MAFIA' if is_mafia else 'INNOCENT'}")


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


@socketio.on('chat_message')
def on_chat(data):
    room_id = data.get('room_id')
    message = data.get('message', '').strip()
    sid = request.sid
    
    if room_id not in active_rooms or sid not in active_rooms[room_id]['players']:
        return
    
    room = active_rooms[room_id]
    player = room['players'][sid]
    
    emit('chat_message', {
        'from': player['nickname'],
        'text': message,
        'ts': datetime.utcnow().isoformat(),
        'type': 'public'
    }, room=room_id)


# ============================================================================
# MAIN
# ============================================================================

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        logger.info("✓ Database initialized")
    
    logger.info("🎭 Server starting on 0.0.0.0:5000")
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
