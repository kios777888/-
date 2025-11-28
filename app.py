# app.py
import os
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room, close_room
import random
import time
from datetime import datetime
import logging
import eventlet

# Use eventlet for better WebSocket performance
eventlet.monkey_patch()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key')

# Configure for Railway
socketio = SocketIO(
    app, 
    cors_allowed_origins="*",
    async_mode='eventlet',
    logger=True,
    engineio_logger=True
)

# Game state management
rooms = {}
players = {}
user_sessions = {}

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
            if player_count < 4:
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
                    emit('role_assigned', role_info, room=player_id)
            
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
            'villager': '👨‍🌾'
        }
        return icons.get(role, '👨‍🌾')

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
            emit('phase_change', {
                'phase': 'night',
                'round': self.game_state['round'],
                'message': f'الليل {self.game_state["round"]} - المافيا تختار ضحيتها'
            }, room=self.id)
            
            logger.info(f"Room {self.id}: Night {self.game_state['round']} started")
            return True
        except Exception as e:
            logger.error(f"Error starting night: {e}")
            return False

    def process_night_actions(self):
        try:
            # Process Mafia votes - Random selection if multiple votes
            mafia_votes = list(self.game_state['mafia_votes'].values())
            if mafia_votes:
                # Randomly select from mafia votes if they disagree
                killed_player = random.choice(mafia_votes)
                self.game_state['killed_tonight'] = killed_player
                logger.info(f"Mafia voted to kill: {killed_player}")

            # Process Doctor vote
            doctor_votes = list(self.game_state['doctor_votes'].values())
            if doctor_votes:
                saved_player = doctor_votes[0]  # Doctor gets one vote
                self.game_state['saved_tonight'] = saved_player
                logger.info(f"Doctor voted to save: {saved_player}")

            # Process Detective vote
            detective_votes = list(self.game_state['detective_votes'].values())
            if detective_votes:
                investigated_player = detective_votes[0]  # Detective gets one vote
                self.game_state['investigated_tonight'] = investigated_player
                
                # Check if investigated player is mafia
                target_player = self.players.get(investigated_player)
                is_mafia = target_player and target_player.get('role') == 'mafia'
                
                # Notify detective
                detective_sid = next((sid for sid, player in self.players.items() 
                                    if player.get('role') == 'detective'), None)
                if detective_sid and target_player:
                    emit('investigation_result', {
                        'target': target_player['nickname'],
                        'is_mafia': is_mafia,
                        'message': f"{target_player['nickname']} is {'مافيا 🔫' if is_mafia else 'بريء ✅'}"
                    }, room=detective_sid)
            return True
        except Exception as e:
            logger.error(f"Error processing night actions: {e}")
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
            self.game_state['killed'] = actual_kill
            
            # Notify all players
            emit('phase_change', {
                'phase': 'day',
                'round': self.game_state['round'],
                'message': message,
                'killed': actual_kill
            }, room=self.id)
            
            logger.info(f"Room {self.id}: Day {self.game_state['round']} started - {message}")
            return True
        except Exception as e:
            logger.error(f"Error starting day: {e}")
            return False

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

@app.route('/api/rooms', methods=['GET'])
def get_rooms():
    try:
        # Clean up empty rooms first
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
        room_id = str(random.randint(1000, 9999))
        
        room = GameRoom(
            room_id=room_id,
            name=data.get('name', 'New Room'),
            max_players=data.get('max_players', 8),
            host_id=request.sid
        )
        
        rooms[room_id] = room
        logger.info(f"Room created: {room_id} by {request.sid}")
        
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
            'players': room.players
        })
    except Exception as e:
        logger.error(f"Error getting room: {e}")
        return jsonify({'error': 'Server error'}), 500

@app.route('/api/auth/guest', methods=['POST'])
def guest_login():
    try:
        guest_id = f"guest_{random.randint(1000, 9999)}"
        username = f"ضيف_{random.randint(100, 999)}"
        
        user_data = {
            'id': guest_id,
            'username': username,
            'isGuest': True
        }
        
        players[request.sid] = user_data
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
                player_name = room.players[request.sid]['nickname']
                leave_room(room_id)
                del room.players[request.sid]
                
                # Notify other players
                emit('player_left', {
                    'player': {'sid': request.sid, 'nickname': player_name},
                    'players': list(room.players.values())
                }, room=room_id)
                
                # If host left, assign new host
                if room.host_id == request.sid and room.players:
                    new_host = next(iter(room.players.keys()))
                    room.host_id = new_host
                    logger.info(f"New host assigned: {new_host}")
    
        # Clean up empty rooms
        cleanup_empty_rooms()
        
        # Remove user session
        if request.sid in user_sessions:
            del user_sessions[request.sid]
            
        # Remove from players
        if request.sid in players:
            del players[request.sid]
            
    except Exception as e:
        logger.error(f"Error during disconnect: {e}")

@socketio.on('join_room')
def handle_join_room(data):
    try:
        room_id = data.get('room_id')
        nickname = data.get('nickname', 'Unknown')
        
        if not room_id:
            emit('error', {'message': 'Room ID is required'})
            return
            
        room = rooms.get(room_id)
        if not room:
            emit('error', {'message': 'Room not found'})
            return
        
        if len(room.players) >= room.max_players:
            emit('error', {'message': 'Room is full'})
            return
        
        # Add player to room
        room.players[request.sid] = {
            'sid': request.sid,
            'nickname': nickname,
            'alive': True,
            'role': None
        }
        
        room.update_activity()
        join_room(room_id)
        
        # Notify all players in room
        emit('player_joined', {
            'player': room.players[request.sid],
            'players': list(room.players.values())
        }, room=room_id)
        
        # Send room update to joining player
        emit('room_update', {
            'room': {
                'id': room.id,
                'name': room.name,
                'max_players': room.max_players,
                'host_id': room.host_id
            },
            'players': list(room.players.values())
        })
        
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
            player_name = room.players[request.sid]['nickname']
            leave_room(room_id)
            del room.players[request.sid]
            
            # Notify other players
            emit('player_left', {
                'player': {'sid': request.sid, 'nickname': player_name},
                'players': list(room.players.values())
            }, room=room_id)
            
            logger.info(f"Player {player_name} left room {room_id}")
            
    except Exception as e:
        logger.error(f"Error leaving room: {e}")

@socketio.on('start_game')
def handle_start_game(data):
    try:
        room_id = data.get('room_id')
        room = rooms.get(room_id)
        
        if not room or room.host_id != request.sid:
            emit('error', {'message': 'Only host can start game'})
            return
        
        if len(room.players) < 3:
            emit('error', {'message': 'Need at least 3 players to start'})
            return
        
        # Assign roles
        if room.assign_roles():
            # Start first night
            room.start_night()
            
            emit('game_started', {
                'phase': 'night',
                'round': 1,
                'players': room.players,
                'message': 'اللعبة بدأت! الليل الأول'
            }, room=room_id)
            
            logger.info(f"Game started in room {room_id}")
        else:
            emit('error', {'message': 'Failed to assign roles'})
            
    except Exception as e:
        logger.error(f"Error starting game: {e}")
        emit('error', {'message': 'Failed to start game'})

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
        
        # Record the vote based on role
        if player['role'] == 'mafia' and action == 'kill':
            room.game_state['mafia_votes'][request.sid] = target_sid
            
            # Notify all mafia members of the vote
            for sid, pl in room.players.items():
                if pl['role'] == 'mafia' and pl['alive']:
                    target_name = room.players[target_sid]['nickname'] if target_sid in room.players else 'Unknown'
                    emit('action_feedback', {
                        'message': f"🔫 {player['nickname']} صوت لقتل {target_name}",
                        'type': 'mafia_vote'
                    }, room=sid)
            
            # Check if all mafia have voted
            alive_mafia = [sid for sid, pl in room.players.items() 
                          if pl['role'] == 'mafia' and pl['alive']]
            if len(room.game_state['mafia_votes']) == len(alive_mafia):
                # All mafia voted, process night actions
                room.process_night_actions()
                
                # Small delay for dramatic effect
                socketio.sleep(2)
                
                # Start day phase
                room.start_day()
                
                # Check for game end
                winner = room.check_game_end()
                if winner:
                    end_game(room, winner)
        
        elif player['role'] == 'doctor' and action == 'heal':
            room.game_state['doctor_votes'][request.sid] = target_sid
            target_name = room.players[target_sid]['nickname'] if target_sid in room.players else 'Unknown'
            
            emit('action_feedback', {
                'message': f"🏥 اخترت علاج {target_name}",
                'type': 'heal'
            })
            
        elif player['role'] == 'detective' and action == 'investigate':
            room.game_state['detective_votes'][request.sid] = target_sid
            
            # Process investigation immediately
            investigated_player = target_sid
            target_player = room.players.get(investigated_player)
            is_mafia = target_player and target_player.get('role') == 'mafia'
            
            if target_player:
                emit('investigation_result', {
                    'target': target_player['nickname'],
                    'is_mafia': is_mafia,
                    'message': f"{target_player['nickname']} هو {'مافيا 🔫' if is_mafia else 'بريء ✅'}"
                })
            
    except Exception as e:
        logger.error(f"Error in night action: {e}")
        emit('error', {'message': 'Failed to process night action'})

def end_game(room, winner):
    try:
        room.game_state['phase'] = 'ended'
        room.game_state['winner'] = winner
        
        if winner == 'mafia':
            message = "🔫 المافيا فازت! الجميع ميت"
        else:
            message = "🎉 القرية فازت! تم القضاء على المافيا"
        
        emit('game_ended', {
            'winner': winner,
            'message': message,
            'players': room.players
        }, room=room.id)
        
        logger.info(f"Game ended in room {room.id}. Winner: {winner}")
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
        
        target_name = room.players[target_sid]['nickname'] if target_sid in room.players else 'Unknown'
        
        # Notify about the vote
        emit('vote_cast', {
            'voter': player['nickname'],
            'voted': target_name
        }, room=room_id)
        
        # Check if voting is complete
        alive_players = [sid for sid, pl in room.players.items() if pl['alive']]
        if len(room.game_state['day_votes']) >= len(alive_players):
            # Process votes and eliminate player with most votes
            vote_count = {}
            for voted_sid in room.game_state['day_votes'].values():
                vote_count[voted_sid] = vote_count.get(voted_sid, 0) + 1
            
            if vote_count:
                executed_sid = max(vote_count.items(), key=lambda x: x[1])[0]
                executed_player = room.players[executed_sid]
                executed_player['alive'] = False
                
                emit('day_outcome', {
                    'executed_sid': executed_sid,
                    'executed_nickname': executed_player['nickname'],
                    'message': f"{executed_player['nickname']} تم إعدامه بالتصويت!"
                }, room=room_id)
                
                # Check for game end
                winner = room.check_game_end()
                if winner:
                    end_game(room, winner)
                else:
                    # Start next night
                    socketio.sleep(3)
                    room.start_night()
                    
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
        
        chat_data = {
            'from': player['nickname'],
            'text': message,
            'ts': datetime.now().isoformat(),
            'type': 'public',
            'id': f"{request.sid}_{datetime.now().timestamp()}"
        }
        
        emit('chat_message', chat_data, room=room_id)
        
    except Exception as e:
        logger.error(f"Error in chat message: {e}")

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
        
        room.update_activity()
        
        # Only send to mafia members
        chat_data = {
            'from': player['nickname'],
            'text': message,
            'ts': datetime.now().isoformat(),
            'type': 'mafia',
            'id': f"mafia_{request.sid}_{datetime.now().timestamp()}"
        }
        
        for sid, pl in room.players.items():
            if pl['role'] == 'mafia' and pl['alive']:
                emit('mafia_chat_message', chat_data, room=sid)
                
    except Exception as e:
        logger.error(f"Error in mafia chat: {e}")

# Utility Functions
def cleanup_empty_rooms():
    """Remove rooms that have been empty for more than 5 minutes"""
    try:
        current_time = datetime.now()
        rooms_to_delete = []
        
        for room_id, room in rooms.items():
            if room.is_empty():
                time_empty = (current_time - room.last_activity).total_seconds()
                if time_empty > 300:  # 5 minutes
                    rooms_to_delete.append(room_id)
                    logger.info(f"Deleting empty room: {room_id}")
        
        for room_id in rooms_to_delete:
            close_room(room_id)
            del rooms[room_id]
    except Exception as e:
        logger.error(f"Error cleaning up rooms: {e}")

# Periodic cleanup task
def periodic_cleanup():
    while True:
        try:
            eventlet.sleep(60)  # Run every minute
            cleanup_empty_rooms()
        except Exception as e:
            logger.error(f"Error in periodic cleanup: {e}")
            eventlet.sleep(60)

# Start cleanup thread
cleanup_thread = eventlet.spawn(periodic_cleanup)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('DEBUG', 'False').lower() == 'true'
    
    logger.info(f"Starting server on port {port}")
    socketio.run(app, host='0.0.0.0', port=port, debug=debug, log_output=debug)
