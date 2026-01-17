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

# --- CONFIGURATION ---
base_dir = Path(__file__).resolve().parent
env_path = base_dir / '.env'
load_dotenv(dotenv_path=env_path)

app = Flask(__name__)
CORS(app)

MONGO_URI = os.environ.get("MONGO_URI")
if not MONGO_URI:
    raise ValueError("CRITICAL ERROR: No MONGO_URI found! Please check your .env file.")

client = MongoClient(MONGO_URI, tlsCAFile=certifi.where(), tlsAllowInvalidCertificates=True)
db = client['variant_db']

# --- HELPER FUNCTIONS ---
def get_bucket(user_id, experiment_id):
    """Deterministic hashing 0-99"""
    raw_string = f"{user_id}:{experiment_id}"
    hash_object = hashlib.md5(raw_string.encode('utf-8'))
    hash_int = int(hash_object.hexdigest(), 16)
    return hash_int % 100

def select_variant(experiment, bucket):
    """Select variant based on traffic percentage"""
    cumulative_threshold = 0
    for variant in experiment['variants']:
        cumulative_threshold += variant.get('traffic_percentage', 0)
        if bucket < cumulative_threshold:
            return variant
    return experiment['variants'][-1]

# --- API ENDPOINTS ---

@app.route('/')
def home():
    return {"status": "Variant Backend is Active", "database": "Connected"}, 200

# 1. CLIENT API (For Android)
@app.route('/api/config', methods=['GET'])
def get_config():
    user_id = request.args.get('userId')
    if not user_id:
        return jsonify({"error": "userId is required"}), 400

    active_experiments = db.experiments.find({"status": "active"})
    config_list = []

    for exp in active_experiments:
        exp_id = str(exp['_id'])
        bucket = get_bucket(user_id, exp_id)
        assigned_variant = select_variant(exp, bucket)
        
        config_list.append({
            "experimentId": exp_id,
            "key": exp['key'],
            "value": assigned_variant['value']
        })

    return jsonify(config_list), 200

@app.route('/api/track', methods=['POST'])
def track_event():
    data = request.json
    # print(f"DEBUG: Track request: {data}")
    
    event_doc = {
        "user_id": data.get('userId'),
        "experiment_id": data.get('experimentId'),
        "variant_name": data.get('variantName'),
        "event_name": data.get('event'), 
        "timestamp": datetime.utcnow()
    }
    
    db.events.insert_one(event_doc)
    return jsonify({"status": "recorded"}), 201

# 2. ADMIN API (For Dashboard)

@app.route('/api/experiments', methods=['POST'])
def create_experiment():
    """Create a new experiment"""
    data = request.json
    if not data or 'variants' not in data:
        return jsonify({"error": "Invalid payload"}), 400

    total_traffic = sum(v.get('traffic_percentage', 0) for v in data['variants'])
    if total_traffic != 100:
        return jsonify({"error": "Traffic percentage must sum to 100"}), 400

    new_experiment = {
        "name": data['name'],
        "key": data['key'],
        "status": "active",
        "variants": data['variants'],
        "created_at": datetime.utcnow()
    }
    result = db.experiments.insert_one(new_experiment)
    return jsonify({"message": "Experiment created", "id": str(result.inserted_id)}), 201

@app.route('/api/admin/experiments', methods=['GET'])
def get_all_experiments():
    """List all experiments"""
    # Includes 'variants' so dashboard can edit traffic
    experiments = list(db.experiments.find({}, {"_id": 0, "name": 1, "key": 1, "status": 1, "variants": 1}))
    return jsonify(experiments), 200

@app.route('/api/admin/experiments/<key>', methods=['DELETE'])
def delete_experiment(key):
    """Delete an experiment"""
    result = db.experiments.delete_one({"key": key})
    if result.deleted_count > 0:
        return jsonify({"message": "Deleted"}), 200
    return jsonify({"error": "Not found"}), 404

@app.route('/api/admin/experiments/<key>', methods=['PUT'])
def update_experiment(key):
    """Update status or traffic split"""
    data = request.json
    update_fields = {}
    
    if 'status' in data:
        update_fields['status'] = data['status']
        
    if 'variants' in data:
        total = sum(v.get('traffic_percentage', 0) for v in data['variants'])
        if total != 100:
            return jsonify({"error": "Traffic must sum to 100"}), 400
        update_fields['variants'] = data['variants']

    if not update_fields:
        return jsonify({"error": "No valid fields"}), 400

    result = db.experiments.update_one({"key": key}, {"$set": update_fields})
    if result.matched_count == 0:
        return jsonify({"error": "Experiment not found"}), 404
        
    return jsonify({"message": "Updated successfully"}), 200

@app.route('/api/admin/stats/<experiment_key>', methods=['DELETE'])
def reset_experiment_stats(experiment_key):
    """Clear all events for an experiment (Restart Test)"""
    experiment = db.experiments.find_one({"key": experiment_key})
    if not experiment:
        return jsonify({"error": "Experiment not found"}), 404
        
    exp_id_str = str(experiment['_id'])
    
    # Delete events matching this experiment ID
    result = db.events.delete_many({
        "$or": [
            {"experiment_id": exp_id_str},
            {"experiment_id": ObjectId(exp_id_str)},
            {"experimentId": exp_id_str}
        ]
    })
    return jsonify({"message": f"Cleared {result.deleted_count} events"}), 200

@app.route('/api/admin/summary/<experiment_key>', methods=['GET'])
def get_experiment_summary(experiment_key):
    """Get chart data"""
    experiment = db.experiments.find_one({"key": experiment_key})
    if not experiment:
        return jsonify({"error": f"Experiment '{experiment_key}' not found"}), 404
    
    exp_id_str = str(experiment['_id'])

    pipeline = [
        {"$match": {"$or": [
            {"experiment_id": exp_id_str},
            {"experiment_id": ObjectId(exp_id_str)}
        ]}},
        {"$group": {
            "_id": "$variant_name",
            "exposures": {"$sum": {"$cond": [{"$eq": ["$event_name", "exposure"]}, 1, 0]}},
            "conversions": {"$sum": {"$cond": [{"$eq": ["$event_name", "conversion"]}, 1, 0]}}
        }}
    ]
    results = list(db.events.aggregate(pipeline))
    
    return jsonify({
        "experiment_name": experiment.get('name'),
        "aggregated_variants": results
    }), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)