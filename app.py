import os
import hashlib
import certifi
from datetime import datetime
from pathlib import Path 
from flask_cors import CORS
from flask import Flask, request, jsonify
from pymongo import MongoClient
from bson.objectid import ObjectId
from dotenv import load_dotenv

# --- BULLETPROOF .ENV LOADER ---
# 1. Get the folder where THIS python file is located
base_dir = Path(__file__).resolve().parent

# 2. Force Python to look for .env right there
env_path = base_dir / '.env'
load_dotenv(dotenv_path=env_path)
# -------------------------------

app = Flask(__name__)
CORS(app)

# --- CONFIGURATION ---
MONGO_URI = os.environ.get("MONGO_URI")

# DEBUG: Print exactly what we found so you know if it's working
print(f"DEBUG: Looking for .env at: {env_path}")
print(f"DEBUG: Found MONGO_URI: {MONGO_URI}")

if not MONGO_URI:
    raise ValueError("CRITICAL ERROR: No MONGO_URI found! Please check your .env file.")

# Connect to MongoDB
# We use certifi to fix SSL certificate errors on Windows
client = MongoClient(MONGO_URI, tlsCAFile=certifi.where(), tlsAllowInvalidCertificates=True)
db = client['variant_db']

# --- HELPER FUNCTIONS ---

def get_bucket(user_id, experiment_id):
    """
    Deterministic Hashing:
    Creates a unique hash for the user+experiment combo.
    Returns an integer between 0 and 99.
    """
    raw_string = f"{user_id}:{experiment_id}"
    hash_object = hashlib.md5(raw_string.encode('utf-8'))
    hash_int = int(hash_object.hexdigest(), 16)
    return hash_int % 100

def select_variant(experiment, bucket):
    """
    Selects the variant based on the user's bucket and cumulative probability.
    """
    cumulative_threshold = 0
    for variant in experiment['variants']:
        cumulative_threshold += variant['traffic_percentage']
        if bucket < cumulative_threshold:
            return variant
    
    # Fallback to the last variant if something goes wrong with math (rounding)
    return experiment['variants'][-1]

# --- API ENDPOINTS ---

@app.route('/api/experiments', methods=['POST'])
def create_experiment():
    """
    ADMIN: Create a new A/B test.
    Input: { "name": "...", "key": "...", "variants": [...] }
    """
    data = request.json
    
    # Basic Validation
    if not data or 'variants' not in data:
        return jsonify({"error": "Invalid payload"}), 400

    # Ensure traffic adds up to 100%
    total_traffic = sum(v.get('traffic_percentage', 0) for v in data['variants'])
    if total_traffic != 100:
        return jsonify({"error": "Traffic percentage must sum to 100"}), 400

    new_experiment = {
        "name": data['name'],
        "key": data['key'], # The key the mobile app listens for (e.g., 'btn_color')
        "status": "active", # active, paused, archived
        "variants": data['variants'],
        "created_at": datetime.utcnow()
    }

    result = db.experiments.insert_one(new_experiment)
    
    return jsonify({
        "message": "Experiment created",
        "id": str(result.inserted_id)
    }), 201

@app.route('/api/config', methods=['GET'])
def get_config():
    user_id = request.args.get('userId')
    if not user_id:
        return jsonify({"error": "userId is required"}), 400

    # 1. Fetch only active experiments
    active_experiments = db.experiments.find({"status": "active"})
    
    # 2. Change response to a LIST to match Android SDK expectation
    config_list = []

    for exp in active_experiments:
        exp_id = str(exp['_id'])
        bucket = get_bucket(user_id, exp_id)
        assigned_variant = select_variant(exp, bucket)
        
        # 3. Use keys that match the Kotlin ExperimentConfig data class
        config_list.append({
            "experimentId": exp_id,
            "key": exp['key'],
            "value": assigned_variant['value']
        })

    return jsonify(config_list), 200
@app.route('/')
def home():
    return {"status": "Variant Backend is Active", "database": "Connected"}, 200
    
@app.route('/api/track', methods=['POST'])
def track_event():
    data = request.json
    
    # DEBUG PRINT: This will show up in your Python terminal
    print(f"DEBUG: Received track request: {data}")
    
    event_doc = {
        "user_id": data.get('userId'),
        "experiment_id": data.get('experimentId'),
        "variant_name": data.get('variantName'),
        "event_name": data.get('event'), 
        "timestamp": datetime.utcnow()
    }
    
    db.events.insert_one(event_doc)
    return jsonify({"status": "recorded"}), 201

@app.route('/api/admin/summary/<experiment_key>', methods=['GET'])
def get_experiment_summary(experiment_key):
    experiment = db.experiments.find_one({"key": experiment_key})
    if not experiment:
        return jsonify({"error": f"Experiment '{experiment_key}' not found"}), 404
    
    exp_id_str = str(experiment['_id'])

    # DEBUG: Let's find EVERY event for this experiment to see what's wrong
    raw_events = list(db.events.find({
        "$or": [
            {"experiment_id": exp_id_str},
            {"experiment_id": ObjectId(exp_id_str)},
            {"experimentId": exp_id_str}
        ]
    }).limit(5))

    # Perform the actual aggregation for the chart
    pipeline = [
        {"$match": {"$or": [
            {"experiment_id": exp_id_str},
            {"experiment_id": ObjectId(exp_id_str)}
        ]}},
        {"$group": {"_id": "$variant_name", "count": {"$sum": 1}}}
    ]
    results = list(db.events.aggregate(pipeline))
    
    return jsonify({
        "experiment_name": experiment.get('name'),
        "experiment_id_we_searched_for": exp_id_str,
        "raw_events_found_count": len(raw_events),
        "sample_events": [{k: str(v) if isinstance(v, ObjectId) else v for k, v in e.items()} for e in raw_events],
        "aggregated_variants": results
    }), 200

if __name__ == '__main__':
    # Using 0.0.0.0 is useful if you want to test from an Emulator later
    app.run(host='0.0.0.0', port=5000, debug=True)
