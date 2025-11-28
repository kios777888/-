import os
from flask import Flask, render_template, request, jsonify, send_from_directory
from flask_socketio import SocketIO, emit, join_room, leave_room, close_room
import random
import time
from datetime import datetime
import logging
import uuid
from threading import Timer

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key')

# Configure for Railway (NO eventlet, use threading)
socketio = SocketIO(
    app, 
    cors_allowed_origins="*",
    async_mode='threading',
    ping_timeout=60,
    ping_interval=25,
    logger=False,
    engineio_logger=False
)

# Game state management
rooms = {}
players = {}
user_sessions = {}
phase_timers = {}  # Store timers for auto-phase transitions

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

NIGHT_DURATION = 30  # seconds
DAY_DURATION = 45    # seconds

class GameRoom:
    def __init__(self, room_id, name, max_players, host_id):
        self.id = room_id
        self.name = name
        self.max_players = max_players
        self.host_id = host_id
        self.players = {}
        self.game_state = {
            'phase': 'waiting',
            'round': 0,
            'mafia_votes': {},
            'doctor_votes': {},
            'detective_votes': {},
            'day_votes': {},
            'killed_tonight': None,
            'saved_tonight': None,
            'investigated_tonight': None
        }
        self.roles_assigned = False
        self.created_at = datetime.now()
        self.last_activity = datetime.now()

    def update_activity(self):
        self.last_activity = datetime.now()

    def is_empty(self):
        return len(self.players) == 0

    def assign_roles(self):
        try:
            player_count = len(self.players)
            if player_count < 3:
                return False

            # Calculate role distribution
            mafia_count = max(1, player_count // 4)
            detective_count = 1 if player_count >= 6 else 0
            doctor_count = 1 if player_count >= 5 else 0
            villager_count = player_count - mafia_count - detective_count - doctor_count

            # Create role pool
            roles = ['mafia'] * mafia_count
            if detective_count:
                roles.append('detective')
            if doctor_count:
                roles.append('doctor')
            roles.extend(['villager'] * villager_count)
            
            random.shuffle(roles)
            
            # Assign roles to players
            for i, (player_id, player) in enumerate(self.players.items()):
                if i < len(roles):
                    role = roles[i]
                    player['role'] = role
                    player['alive'] = True
                    
                    # Role descriptions in Arabic
                    role_info = {
                        'role': role,
                        'role_ar': self.get_role_arabic(role),
                        'icon': self.get_role_icon(role),
                        'color': self.get_role_color(role)
                    }
                    
                    # Notify player of their role
                    socketio.emit('role_assigned', role_info, room=player_id)
            
            self.roles_assigned = True
            return True
        except Exception as e:
            logger.error(f"Error assigning roles: {e}")
            return False

    def get_role_arabic(self, role):
        roles_ar = {
            'mafia': 'مافيا',
            'detective': 'محقق',
            'doctor': 'طبيب',
            'villager': 'مواطن'
        }
        return roles_ar.get(role, 'مواطن')

    def get_role_icon(self, role):
        icons = {
            'mafia': '🔫',
            'detective': '🔍',
            'doctor': '🏥',
            'villager': '👥'
        }
        return icons.get(role, '👥')

    def get_role_color(self, role):
        colors = {
            'mafia': '#dc2626',
            'detective': '#2563eb',
            'doctor': '#16a34a',
            'villager': '#6b7280'
        }
        return colors.get(role, '#6b7280')

    def start_night(self):
        try:
            self.game_state['phase'] = 'night'
            self.game_state['round'] += 1
            self.game_state['mafia_votes'] = {}
            self.game_state['doctor_votes'] = {}
            self.game_state['detective_votes'] = {}
            self.game_state['killed_tonight'] = None
            self.game_state['saved_tonight'] = None
            self.game_state['investigated_tonight'] = None
            
            # Notify all players
            socketio.emit('phase_change', {
                'phase': 'night',
                'round': self.game_state['round'],
                'message': f'الليل {self.game_state["round"]} - المافيا تختار ضحيتها'
            }, room=self.id)
            
            logger.info(f"Room {self.id}: Night {self.game_state['round']} started")
            
            # Schedule auto-transition to day
            self.schedule_phase_transition('day', NIGHT_DURATION)
            return True
        except Exception as e:
            logger.error(f"Error starting night: {e}")
            return False

    def start_day(self):
        try:
            # Determine if anyone was killed
            killed_player = self.game_state['killed_tonight']
            saved_player = self.game_state['saved_tonight']
            
            actual_kill = None
            if killed_player and killed_player != saved_player:
                # Kill the player
                if killed_player in self.players:
                    self.players[killed_player]['alive'] = False
                    actual_kill = killed_player
                    logger.info(f"Player {killed_player} was killed")
            
            # Build message
            if actual_kill:
                killed_name = self.players[actual_kill]['nickname']
                message = f"☀️ الصباح - {killed_name} تم قتله الليلة!"
            elif killed_player and saved_player:
                saved_name = self.players[saved_player]['nickname']
                message = f"☀️ الصباح - الطبيب أنقذ {saved_name}!"
            else:
                message = "☀️ الصباح - ليلة هادئة، لم يمت أحد"
            
            # Update game state
            self.game_state['phase'] = 'day'
            self.game_state['day_votes'] = {}
            self.game_state['killed'] = actual_kill
            
            # Notify all players
            socketio.emit('phase_change', {
                'phase': 'day',
                'round': self.game_state['round'],
                'message': message,
                'killed': actual_kill
            }, room=self.id)
            
            logger.info(f"Room {self.id}: Day {self.game_state['round']} started - {message}")
            
            # Schedule auto-transition to night
            self.schedule_phase_transition('night', DAY_DURATION)
            return True
        except Exception as e:
            logger.error(f"Error starting day: {e}")
            return False

    def schedule_phase_transition(self, next_phase, delay):
        """Schedule automatic phase transition"""
        try:
            # Cancel existing timer
            if self.id in phase_timers:
                phase_timers[self.id].cancel()
            
            def do_transition():
                try:
                    room = rooms.get(self.id)
                    if not room:
                        return
                    
                    # Check win condition
                    winner = room.check_game_end()
                    if winner:
                        end_game(room, winner)
                        return
                    
                    # Transition to next phase
                    if next_phase == 'day':
                        room.start_day()
                    elif next_phase == 'night':
                        room.start_night()
                except Exception as e:
                    logger.error(f"Error in phase transition: {e}")
            
            timer = Timer(delay, do_transition)
            timer.daemon = True
            timer.start()
            phase_timers[self.id] = timer
            
            logger.info(f"Phase transition scheduled: {next_phase} in {delay}s for room {self.id}")
        except Exception as e:
            logger.error(f"Error scheduling phase transition: {e}")

    def check_game_end(self):
        try:
            alive_players = [p for p in self.players.values() if p['alive']]
            alive_mafia = [p for p in alive_players if p['role'] == 'mafia']
            alive_villagers = [p for p in alive_players if p['role'] != 'mafia']
            
            if len(alive_mafia) == 0:
                return 'villagers'
            elif len(alive_mafia) >= len(alive_villagers):
                return 'mafia'
            return None
        except Exception as e:
            logger.error(f"Error checking game end: {e}")
            return None

# Routes
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/health')
def health():
    return jsonify({'status': 'healthy', 'rooms': len(rooms), 'players': len(players)})

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

@app.route('/api/rooms', methods=['GET'])
def get_rooms():
    try:
        cleanup_empty_rooms()
        
        room_list = []
        for room_id, room in rooms.items():
            room_list.append({
                'id': room_id,
                'name': room.name,
                'player_count': len(room.players),
                'max_players': room.max_players,
                'host_id': room.host_id
            })
        
        return jsonify(room_list)
    except Exception as e:
        logger.error(f"Error getting rooms: {e}")
        return jsonify([])

@app.route('/api/rooms', methods=['POST'])
def create_room():
    try:
        data = request.get_json() or {}
        room_id = str(uuid.uuid4())[:8]
        
        room = GameRoom(
            room_id=room_id,
            name=data.get('name', 'غرفة جديدة'),
            max_players=int(data.get('max_players', 8)),
            host_id=request.sid
        )
        
        rooms[room_id] = room
        logger.info(f"Room created: {room_id}")
        
        return jsonify({
            'id': room_id,
            'name': room.name,
            'max_players': room.max_players,
            'host_id': room.host_id
        })
    except Exception as e:
        logger.error(f"Error creating room: {e}")
        return jsonify({'error': 'Failed to create room'}), 500

@app.route('/api/rooms/<room_id>')
def get_room(room_id):
    try:
        room = rooms.get(room_id)
        if not room:
            return jsonify({'error': 'Room not found'}), 404
        
        return jsonify({
            'id': room.id,
            'name': room.name,
            'max_players': room.max_players,
            'host_id': room.host_id,
            'players': list(room.players.values())
        })
    except Exception as e:
        logger.error(f"Error getting room: {e}")
        return jsonify({'error': 'Server error'}), 500

@app.route('/api/auth/guest', methods=['POST'])
def guest_login():
    try:
        guest_id = str(uuid.uuid4())[:8]
        username = f"Guest_{random.randint(100, 999)}"
        
        user_data = {
            'id': guest_id,
            'username': username,
            'isGuest': True
        }
        
        return jsonify({'user': user_data, 'token': f"guest_{guest_id}"})
    except Exception as e:
        logger.error(f"Error in guest login: {e}")
        return jsonify({'error': 'Login failed'}), 500

# Socket Events
@socketio.on('connect')
def handle_connect():
    logger.info(f"Client connected: {request.sid}")
    user_sessions[request.sid] = {
        'connected': True,
        'connected_at': datetime.now()
    }

@socketio.on('disconnect')
def handle_disconnect():
    logger.info(f"Client disconnected: {request.sid}")
    
    try:
        # Remove player from rooms
        for room_id, room in list(rooms.items()):
            if request.sid in room.players:
                del room.players[request.sid]
                leave_room(room_id)
                
                # Notify others
                socketio.emit('player_left', {
                    'player_count': len(room.players)
                }, room=room_id)
        
        # Cleanup
        cleanup_empty_rooms()
        
        if request.sid in user_sessions:
            del user_sessions[request.sid]
    except Exception as e:
        logger.error(f"Error during disconnect: {e}")

@socketio.on('join_room')
def handle_join_room(data):
    try:
        room_id = data.get('room_id')
        nickname = data.get('nickname', 'Player')
        
        if not room_id or room_id not in rooms:
            emit('error', {'message': 'Room not found'})
            return
        
        room = rooms[room_id]
        
        if len(room.players) >= room.max_players:
            emit('error', {'message': 'Room is full'})
            return
        
        # Add player
        room.players[request.sid] = {
            'sid': request.sid,
            'nickname': nickname,
            'alive': True,
            'role': None
        }
        
        room.update_activity()
        join_room(room_id)
        
        # Notify all
        socketio.emit('player_joined', {
            'player': room.players[request.sid],
            'players': list(room.players.values())
        }, room=room_id)
        
        logger.info(f"Player {nickname} joined room {room_id}")
        
    except Exception as e:
        logger.error(f"Error joining room: {e}")
        emit('error', {'message': 'Failed to join room'})

@socketio.on('leave_room')
def handle_leave_room(data):
    try:
        room_id = data.get('room_id')
        room = rooms.get(room_id)
        
        if room and request.sid in room.players:
            del room.players[request.sid]
            leave_room(room_id)
            
            socketio.emit('player_left', {
                'player_count': len(room.players)
            }, room=room_id)
        
        cleanup_empty_rooms()
    except Exception as e:
        logger.error(f"Error leaving room: {e}")

@socketio.on('start_game')
def handle_start_game(data):
    try:
        room_id = data.get('room_id')
        room = rooms.get(room_id)
        
        if not room or room.host_id != request.sid:
            emit('error', {'message': 'Only host can start'})
            return
        
        if len(room.players) < 3:
            emit('error', {'message': 'Need 3+ players'})
            return
        
        # Assign roles
        if room.assign_roles():
            room.start_night()
            socketio.emit('game_started', {
                'phase': 'night',
                'round': 1,
                'players': list(room.players.values()),
                'message': 'اللعبة بدأت!'
            }, room=room_id)
            logger.info(f"Game started in {room_id}")
        else:
            emit('error', {'message': 'Failed to start game'})
    except Exception as e:
        logger.error(f"Error starting game: {e}")
        emit('error', {'message': 'Server error'})

@socketio.on('night_action')
def handle_night_action(data):
    try:
        room_id = data.get('room_id')
        action = data.get('action')
        target_sid = data.get('target_sid')
        
        room = rooms.get(room_id)
        if not room or room.game_state['phase'] != 'night':
            return
        
        player = room.players.get(request.sid)
        if not player or not player['alive']:
            return
        
        room.update_activity()
        
        if player['role'] == 'mafia' and action == 'kill':
            room.game_state['mafia_votes'][request.sid] = target_sid
            emit('action_feedback', {'message': f"🔫 تم اختيار الهدف"})
        
        elif player['role'] == 'doctor' and action == 'heal':
            room.game_state['doctor_votes'][request.sid] = target_sid
            emit('action_feedback', {'message': f"🏥 تم اختيار من سيتم علاجه"})
        
        elif player['role'] == 'detective' and action == 'investigate':
            room.game_state['detective_votes'][request.sid] = target_sid
            
            target_player = room.players.get(target_sid)
            is_mafia = target_player and target_player.get('role') == 'mafia'
            
            emit('investigation_result', {
                'target': target_player['nickname'] if target_player else 'Unknown',
                'is_mafia': is_mafia,
                'message': 'تم التحقيق'
            })
    except Exception as e:
        logger.error(f"Error in night action: {e}")

def end_game(room, winner):
    try:
        room.game_state['phase'] = 'ended'
        
        message = "🔫 المافيا فازت!" if winner == 'mafia' else "🎉 القرية فازت!"
        
        socketio.emit('game_ended', {
            'winner': winner,
            'message': message,
            'players': list(room.players.values())
        }, room=room.id)
        
        logger.info(f"Game ended in {room.id}. Winner: {winner}")
    except Exception as e:
        logger.error(f"Error ending game: {e}")

@socketio.on('day_vote')
def handle_day_vote(data):
    try:
        room_id = data.get('room_id')
        target_sid = data.get('vote_for_sid')
        
        room = rooms.get(room_id)
        if not room or room.game_state['phase'] != 'day':
            return
        
        player = room.players.get(request.sid)
        if not player or not player['alive']:
            return
        
        room.update_activity()
        room.game_state['day_votes'][request.sid] = target_sid
        
        socketio.emit('vote_cast', {
            'voter': player['nickname'],
            'voted': room.players[target_sid]['nickname']
        }, room=room_id)
    except Exception as e:
        logger.error(f"Error in day vote: {e}")

@socketio.on('chat_message')
def handle_chat_message(data):
    try:
        room_id = data.get('room_id')
        message = data.get('message', '').strip()
        
        if not message:
            return
        
        room = rooms.get(room_id)
        if not room:
            return
        
        player = room.players.get(request.sid)
        if not player:
            return
        
        room.update_activity()
        
        socketio.emit('chat_message', {
            'from': player['nickname'],
            'text': message,
            'ts': datetime.now().isoformat(),
            'type': 'public'
        }, room=room_id)
    except Exception as e:
        logger.error(f"Error in chat: {e}")

@socketio.on('mafia_chat_message')
def handle_mafia_chat(data):
    try:
        room_id = data.get('room_id')
        message = data.get('message', '').strip()
        
        if not message:
            return
        
        room = rooms.get(room_id)
        if not room:
            return
        
        player = room.players.get(request.sid)
        if not player or player['role'] != 'mafia' or not player['alive']:
            return
        
        # Send to all mafia
        for sid, p in room.players.items():
            if p['role'] == 'mafia' and p['alive']:
                socketio.emit('mafia_chat_message', {
                    'from': player['nickname'],
                    'text': message,
                    'ts': datetime.now().isoformat(),
                    'type': 'mafia'
                }, room=sid)
    except Exception as e:
        logger.error(f"Error in mafia chat: {e}")

def cleanup_empty_rooms():
    """Remove empty rooms"""
    try:
        current_time = datetime.now()
        rooms_to_delete = []
        
        for room_id, room in rooms.items():
            if room.is_empty():
                time_empty = (current_time - room.last_activity).total_seconds()
                if time_empty > 300:
                    rooms_to_delete.append(room_id)
                    
                    # Cancel timer
                    if room_id in phase_timers:
                        phase_timers[room_id].cancel()
                        del phase_timers[room_id]
        
        for room_id in rooms_to_delete:
            close_room(room_id)
            del rooms[room_id]
            logger.info(f"Deleted empty room: {room_id}")
    except Exception as e:
        logger.error(f"Cleanup error: {e}")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    logger.info(f"Starting server on port {port}")
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
