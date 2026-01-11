from pymongo import MongoClient
import certifi

# Use your actual MONGO_URI here
MONGO_URI = "mongodb+srv://markoori6_db_user:mn03czMdLuKW82Gg@varient.pcqngem.mongodb.net/variant_db?appName=varient"
client = MongoClient(MONGO_URI, tlsCAFile=certifi.where(), tlsAllowInvalidCertificates=True)
db = client['variant_db']

# DANGER: This deletes all data in these collections
print("Deleting all experiments...")
db.experiments.delete_many({})
print("Deleting all events...")
db.events.delete_many({})
print("Database is now clean!")