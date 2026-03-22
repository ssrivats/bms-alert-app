"""
BookMyShow Seat Alert Backend — v18 (Extension-Driven Architecture)

The Chrome extension now owns the monitoring loop.
This backend handles:
  - Monitor CRUD (create / read / list / stop)
  - Status updates from the extension
  - WhatsApp alerts via Twilio
  - Redis persistence
"""

import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime

from flask import Flask, jsonify, request
from flask_cors import CORS
from twilio.rest import Client

app = Flask(__name__)
CORS(app)

# ────────────────────────────────────────────────────────────────────────────
# Logging
# ────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────────────
# Configuration
# ────────────────────────────────────────────────────────────────────────────
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID', '')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN', '')
TWILIO_WHATSAPP_FROM = os.getenv('TWILIO_WHATSAPP_FROM', '')

REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379')
MAX_ACTIVE_PER_PHONE = 5

# ────────────────────────────────────────────────────────────────────────────
# Initialize Twilio (if credentials provided)
# ────────────────────────────────────────────────────────────────────────────
twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    try:
        twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        logger.info('Twilio client initialized')
    except Exception as e:
        logger.error(f'Failed to initialize Twilio: {e}')

# ────────────────────────────────────────────────────────────────────────────
# Redis Setup (in-memory fallback for demo)
# ────────────────────────────────────────────────────────────────────────────
try:
    import redis
    redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    redis_client.ping()
    logger.info('Connected to Redis')
except ImportError:
    logger.warning('redis not installed, using in-memory storage')
    redis_client = None
except Exception as e:
    logger.warning(f'Failed to connect to Redis: {e}, using in-memory storage')
    redis_client = None

# In-memory fallback storage
_in_memory_storage = {}

# ────────────────────────────────────────────────────────────────────────────
# Storage Helpers
# ────────────────────────────────────────────────────────────────────────────
def _save_monitor(monitor_id, data):
    """Save monitor record to Redis or in-memory storage."""
    try:
        if redis_client:
            redis_client.set(f'monitor:{monitor_id}', json.dumps(data))
        else:
            _in_memory_storage[f'monitor:{monitor_id}'] = data
        logger.info(f'Saved monitor {monitor_id}')
    except Exception as e:
        logger.error(f'Error saving monitor {monitor_id}: {e}')

def _load_monitor(monitor_id):
    """Load monitor record from Redis or in-memory storage."""
    try:
        if redis_client:
            data = redis_client.get(f'monitor:{monitor_id}')
            return json.loads(data) if data else None
        else:
            return _in_memory_storage.get(f'monitor:{monitor_id}')
    except Exception as e:
        logger.error(f'Error loading monitor {monitor_id}: {e}')
        return None

def _load_all_monitors():
    """Load all monitors."""
    try:
        if redis_client:
            keys = redis_client.keys('monitor:*')
            monitors = []
            for key in keys:
                data = redis_client.get(key)
                if data:
                    monitors.append(json.loads(data))
            return monitors
        else:
            return [v for k, v in _in_memory_storage.items() if k.startswith('monitor:')]
    except Exception as e:
        logger.error(f'Error loading all monitors: {e}')
        return []

def _add_log(monitor_id, message):
    """Add log entry to monitor."""
    try:
        monitor = _load_monitor(monitor_id)
        if not monitor:
            return
        
        if 'logs' not in monitor:
            monitor['logs'] = []
        
        log_entry = {
            'time': datetime.utcnow().isoformat() + 'Z',
            'message': message
        }
        monitor['logs'].append(log_entry)
        # Keep only last 50 logs
        monitor['logs'] = monitor['logs'][-50:]
        
        _save_monitor(monitor_id, monitor)
    except Exception as e:
        logger.error(f'Error adding log for {monitor_id}: {e}')

# ────────────────────────────────────────────────────────────────────────────
# WhatsApp Alert via Twilio
# ────────────────────────────────────────────────────────────────────────────
def _send_alert(monitor_id, booking_url=''):
    """Send WhatsApp alert via Twilio."""
    try:
        monitor = _load_monitor(monitor_id)
        if not monitor:
            logger.error(f'Monitor {monitor_id} not found for alert')
            return False
        
        if not twilio_client:
            logger.warning(f'Twilio client not initialized, skipping alert for {monitor_id}')
            return False
        
        phone = monitor.get('phone', '')
        movie = monitor.get('movie', 'Unknown show')
        venue = monitor.get('theatre', '')
        showtime = monitor.get('showtime', '')
        rows = monitor.get('rows', '')
        
        # Build message
        msg_parts = [f'🎬 {movie}']
        if venue:
            msg_parts.append(f'📍 {venue}')
        if showtime:
            msg_parts.append(f'🕐 {showtime}')
        if rows:
            msg_parts.append(f'🪑 Rows: {rows}')
        msg_parts.append('\n✅ Seats available! Click link to book:')
        if booking_url:
            msg_parts.append(booking_url)
        else:
            msg_parts.append('https://in.bookmyshow.com')
        
        message_text = '\n'.join(msg_parts)
        
        # Send via Twilio WhatsApp
        message = twilio_client.messages.create(
            from_=f'whatsapp:{TWILIO_WHATSAPP_FROM}',
            body=message_text,
            to=f'whatsapp:{phone}'
        )
        
        logger.info(f'WhatsApp alert sent for {monitor_id} to {phone}')
        _add_log(monitor_id, f'✅ WhatsApp alert sent to {phone}')
        return True
    except Exception as e:
        logger.error(f'Error sending alert for {monitor_id}: {e}')
        _add_log(monitor_id, f'❌ Failed to send alert: {str(e)}')
        return False

# ────────────────────────────────────────────────────────────────────────────
# Smart polling interval based on showtime
# ────────────────────────────────────────────────────────────────────────────
def _smart_interval(showtime_str, default=20):
    """
    Calculate smart polling interval based on showtime.
    Closer to showtime → more frequent polling.
    """
    try:
        if not showtime_str:
            return default
        
        # Parse showtime (e.g., "10:30 PM")
        match = re.match(r'(\d{1,2}):(\d{2})\s*(AM|PM)', showtime_str, re.IGNORECASE)
        if not match:
            return default
        
        hours, mins, period = int(match.group(1)), int(match.group(2)), match.group(3).upper()
        
        # Convert to 24-hour
        if period == 'PM' and hours != 12:
            hours += 12
        elif period == 'AM' and hours == 12:
            hours = 0
        
        # Get time until showtime
        now = datetime.now()
        show_time = datetime.now().replace(hour=hours, minute=mins, second=0, microsecond=0)
        
        # If showtime already passed, assume next day
        if show_time <= now:
            return default
        
        diff_mins = (show_time - now).total_seconds() / 60
        
        # Smart intervals
        if diff_mins < 30:
            return 5
        elif diff_mins < 120:
            return 10
        elif diff_mins < 360:
            return 20
        else:
            return 30
    except Exception as e:
        logger.error(f'Error calculating interval for "{showtime_str}": {e}')
        return default

# ────────────────────────────────────────────────────────────────────────────
# Normalize row labels
# ────────────────────────────────────────────────────────────────────────────
def _normalize_row_label(value):
    """Normalize row label for matching."""
    if not value:
        return ''
    return str(value).strip().upper()

# ────────────────────────────────────────────────────────────────────────────
# REST API Endpoints
# ────────────────────────────────────────────────────────────────────────────

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint."""
    try:
        monitors = _load_all_monitors()
        active_count = len([m for m in monitors if m.get('status') in [
            'waiting_for_session', 'session_resolved', 'checking_rows', 'monitoring'
        ]])
        
        return jsonify({
            'status': 'ok',
            'service': 'bms-alert-backend',
            'version': 'v18',
            'total_monitors': len(monitors),
            'active_monitors': active_count,
            'twilio_ready': twilio_client is not None,
            'redis_ready': redis_client is not None,
            'timestamp': datetime.utcnow().isoformat() + 'Z'
        })
    except Exception as e:
        logger.error(f'Health check error: {e}')
        return jsonify({'status': 'error', 'error': str(e)}), 500

@app.route('/api/monitors', methods=['GET'])
def get_monitors():
    """List all monitors."""
    try:
        monitors = _load_all_monitors()
        monitors.sort(key=lambda m: m.get('created_at', ''), reverse=True)
        return jsonify({'monitors': monitors})
    except Exception as e:
        logger.error(f'Error listing monitors: {e}')
        return jsonify({'error': str(e)}), 500

@app.route('/api/monitor/<monitor_id>', methods=['GET'])
def get_monitor(monitor_id):
    """Get single monitor details."""
    try:
        monitor = _load_monitor(monitor_id)
        if not monitor:
            return jsonify({'error': 'Monitor not found'}), 404
        return jsonify(monitor)
    except Exception as e:
        logger.error(f'Error getting monitor {monitor_id}: {e}')
        return jsonify({'error': str(e)}), 500

@app.route('/api/monitor', methods=['POST'])
def create_monitor():
    """
    Create new monitor.
    
    Listing mode requires:
      - listing_url, event_code, venue_code, date, target_showtime, phone
    
    Session mode requires:
      - booking_url, phone
    """
    try:
        data = request.get_json()
        
        # Validate phone
        phone = data.get('phone', '').strip()
        if not phone:
            return jsonify({'error': 'Phone number required'}), 400
        
        # Check max active per phone
        monitors = _load_all_monitors()
        active_for_phone = len([
            m for m in monitors
            if m.get('phone') == phone and m.get('status') in [
                'waiting_for_session', 'session_resolved', 'checking_rows', 'monitoring'
            ]
        ])
        if active_for_phone >= MAX_ACTIVE_PER_PHONE:
            return jsonify({
                'error': f'Max {MAX_ACTIVE_PER_PHONE} active monitors per phone'
            }), 400
        
        mode = data.get('mode', 'session')
        monitor_id = str(uuid.uuid4())[:8]
        
        monitor = {
            'id': monitor_id,
            'mode': mode,
            'phone': phone,
            'movie': data.get('movie', 'Unknown show'),
            'theatre': data.get('theatre', ''),
            'showtime': data.get('showtime', ''),
            'status': 'waiting_for_session' if mode == 'listing' else 'monitoring',
            'created_at': datetime.utcnow().isoformat() + 'Z',
            'logs': []
        }
        
        # Listing mode fields
        if mode == 'listing':
            monitor.update({
                'listing_url': data.get('listing_url', ''),
                'event_code': data.get('event_code', ''),
                'venue_code': data.get('venue_code', ''),
                'date': data.get('date', ''),
                'target_showtime': data.get('target_showtime', ''),
                'preferred_rows': data.get('preferred_rows', []),
            })
        
        # Session mode fields
        if mode == 'session':
            monitor.update({
                'booking_url': data.get('booking_url', ''),
                'show_id': data.get('show_id', ''),
                'preferred_rows': data.get('preferred_rows', []),
            })
        
        # Add all fields from payload for extension compatibility
        monitor['rows'] = ','.join(data.get('preferred_rows', []))
        monitor['poll_interval'] = data.get('poll_interval', 20)
        monitor['booking_url'] = data.get('booking_url', '')
        monitor['sessionId'] = data.get('show_id', '')
        monitor['listingUrl'] = data.get('listing_url', '')
        monitor['eventCode'] = data.get('event_code', '')
        monitor['venueCode'] = data.get('venue_code', '')
        monitor['targetShowtime'] = data.get('target_showtime', '')
        monitor['preferredRows'] = data.get('preferred_rows', [])
        
        _save_monitor(monitor_id, monitor)
        _add_log(monitor_id, f'📊 Monitor created in {mode} mode')
        
        logger.info(f'Created monitor {monitor_id} in {mode} mode')
        
        return jsonify({
            'monitor_id': monitor_id,
            'status': monitor['status'],
            'message': f'Monitor created. Poll interval: {monitor["poll_interval"]}s'
        }), 201
    except Exception as e:
        logger.error(f'Error creating monitor: {e}')
        return jsonify({'error': str(e)}), 500

@app.route('/api/monitor/<monitor_id>/update', methods=['POST'])
def update_monitor_status(monitor_id):
    """
    Receive polling updates from Chrome extension.
    
    Body:
      - status: new status
      - message: log message
      - session_id: (optional) session found
      - booking_url: (optional) updated booking URL
    """
    try:
        data = request.get_json()
        monitor = _load_monitor(monitor_id)
        
        if not monitor:
            return jsonify({'error': 'Monitor not found'}), 404
        
        # Update status
        new_status = data.get('status')
        if new_status:
            monitor['status'] = new_status
        
        # Add log
        message = data.get('message', '')
        if message:
            _add_log(monitor_id, message)
        
        # Update session info if provided
        if data.get('session_id'):
            monitor['sessionId'] = data['session_id']
        if data.get('booking_url'):
            monitor['bookingUrl'] = data['booking_url']
            monitor['booking_url'] = data['booking_url']
        
        _save_monitor(monitor_id, monitor)
        
        return jsonify({'ok': True})
    except Exception as e:
        logger.error(f'Error updating monitor {monitor_id}: {e}')
        return jsonify({'error': str(e)}), 500

@app.route('/api/monitor/<monitor_id>/alert', methods=['POST'])
def trigger_alert(monitor_id):
    """
    Trigger WhatsApp alert for this monitor.
    Called by Chrome extension when seats are found.
    """
    try:
        monitor = _load_monitor(monitor_id)
        if not monitor:
            return jsonify({'error': 'Monitor not found'}), 404
        
        booking_url = monitor.get('booking_url', monitor.get('bookingUrl', ''))
        
        # Send WhatsApp
        success = _send_alert(monitor_id, booking_url)

        # Only mark alert_sent if Twilio actually succeeded.
        # On failure keep status as-is so the extension can retry.
        if success:
            monitor['status'] = 'alert_sent'
            _save_monitor(monitor_id, monitor)
            return jsonify({
                'ok': True,
                'status': 'alert_sent',
                'message': 'Alert sent'
            })
        else:
            monitor['status'] = 'alert_failed'
            _add_log(monitor_id, '⚠️ Alert failed — will retry on next poll')
            _save_monitor(monitor_id, monitor)
            return jsonify({
                'ok': False,
                'status': 'alert_failed',
                'message': 'Alert delivery failed — monitor will retry'
            })
    except Exception as e:
        logger.error(f'Error triggering alert for {monitor_id}: {e}')
        return jsonify({'error': str(e)}), 500

@app.route('/api/monitor/<monitor_id>/stop', methods=['POST'])
def stop_monitor(monitor_id):
    """Stop monitoring."""
    try:
        monitor = _load_monitor(monitor_id)
        if not monitor:
            return jsonify({'error': 'Monitor not found'}), 404
        
        monitor['status'] = 'stopped'
        _save_monitor(monitor_id, monitor)
        _add_log(monitor_id, '⏹️ Monitor stopped by user')
        
        logger.info(f'Stopped monitor {monitor_id}')
        
        return jsonify({'ok': True})
    except Exception as e:
        logger.error(f'Error stopping monitor {monitor_id}: {e}')
        return jsonify({'error': str(e)}), 500

@app.route('/api/test-whatsapp', methods=['POST'])
def test_whatsapp():
    """Test WhatsApp integration."""
    try:
        data = request.get_json()
        phone = data.get('phone', '').strip()
        
        if not phone:
            return jsonify({'error': 'Phone required'}), 400
        
        if not twilio_client:
            return jsonify({'error': 'Twilio not configured'}), 500
        
        message = twilio_client.messages.create(
            from_=f'whatsapp:{TWILIO_WHATSAPP_FROM}',
            body='🧪 Test message from BookMyShow Seat Alert',
            to=f'whatsapp:{phone}'
        )
        
        logger.info(f'Test WhatsApp sent to {phone}')
        
        return jsonify({
            'ok': True,
            'message_sid': message.sid,
            'to': phone
        })
    except Exception as e:
        logger.error(f'Error sending test WhatsApp: {e}')
        return jsonify({'error': str(e)}), 500

# ────────────────────────────────────────────────────────────────────────────
# Error handlers
# ────────────────────────────────────────────────────────────────────────────
@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Not found'}), 404

@app.errorhandler(500)
def internal_error(error):
    logger.error(f'Internal error: {error}')
    return jsonify({'error': 'Internal server error'}), 500

# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    logger.info('=== BookMyShow Seat Alert Backend v18 ===')
    logger.info(f'Redis: {"✅" if redis_client else "❌ (in-memory)"}')
    logger.info(f'Twilio: {"✅" if twilio_client else "❌"}')
    
    port = int(os.getenv('PORT', 5000))
    app.run(
        host='0.0.0.0',
        port=port,
        debug=os.getenv('DEBUG', 'False') == 'True',
        threaded=True
    )
