import os
import json
from dotenv import load_dotenv
from RTN import RTN
from RTN.models import SettingsModel, DefaultRanking

# Load environment variables from .env file
load_dotenv()

# Load settings from JSON file
with open('settings.json', 'r') as file:
    settings_data = json.load(file)

# Define the settings for torrent ranking
settings = SettingsModel(**settings_data)

# Initialize RTN with the settings
rtn = RTN(settings=settings, ranking_model=DefaultRanking())

# Verify the settings and RTN instance
print("Settings loaded successfully:")
print(settings)
print("\nRTN instance initialized successfully:")
print(rtn)