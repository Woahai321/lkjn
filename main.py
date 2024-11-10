# =============================================================================
# Soluify.com  |  Your #1 IT Problem Solver  |  {SeerrLitee v0.3 Refactor}
# =============================================================================
#  __         _
# (_  _ |   .(_
# __)(_)||_||| \/
#              /
# © 2024
# -----------------------------------------------------------------------------

from fastapi import FastAPI, Request
from loguru import logger
from models import OverseerrWebhook
from utils import start_processing_queue, get_imdb_id_from_trakt, query_torrentio, check_rd_availability, add_torrent_and_select_files
from pydantic import ValidationError
from typing import Dict, Any
from RTN import RTN
from RTN.models import SettingsModel, CustomRank, DefaultRanking
from RTN.exceptions import GarbageTorrent
from settings import rtn, settings
import sys


# Initialize FastAPI app
app = FastAPI()

# FastAPI endpoint to receive the webhook payload from Jellyseer
@app.post("/jellyseer-webhook/")
async def jellyseer_webhook(request: Request) -> Dict[str, Any]:
    try:
        response = await request.json()

        # Check for test notification
        if response.get("subject") == "Test Notification":
            logger.info("Received test notification, Overseerr configured properly")
            return {"success": True, "message": "Test notification received successfully"}

        # Validate the incoming payload using the Pydantic model
        req = OverseerrWebhook.model_validate(response)

    except (Exception, ValidationError) as e:
        logger.error(f"Failed to process request: {e}")
        return {"success": False, "message": str(e)}

    try:
        # Step 1: Extract the tmdbId from the payload's media object
        tmdb_id = req.media.tmdbId
        media_type = req.media.media_type
        logger.info(f"Received tmdbId: {tmdb_id} and media type: {media_type} from Jellyseerr webhook.")

        # Step 3: Fetch IMDb ID using Trakt API
        imdb_id = get_imdb_id_from_trakt(tmdb_id, media_type)
        logger.info(f"Fetching IMDb ID using Trakt API for tmdbId: {tmdb_id}...")
        if not imdb_id:
            return {"success": False, "message": "IMDb ID not found"}

        logger.info(f"IMDb ID found: {imdb_id}")

        # Step 4: Query Torrentio API to get torrents
        logger.info(f"Querying Torrentio API for torrents with IMDb ID: {imdb_id}...")
        torrentio_results = query_torrentio(imdb_id, media_type)
        if not torrentio_results or not torrentio_results.get('streams'):
            logger.error("No torrents found on Torrentio.")
            return {"success": False, "message": "No torrents found"}

        # Step 5: Check Real-Debrid availability and rank torrents
        logger.info("Checking Real-Debrid availability and ranking torrents...")

        for stream in torrentio_results['streams']:
            info_hash = stream.get('infoHash')
            title = stream.get('title')
            file_idx = stream.get('fileIdx')
            
            if info_hash and title and file_idx is not None:
                rd_availability = check_rd_availability(info_hash)
                
                if rd_availability:
                    try:
                        # Rank the torrent using RTN
                        torrent = rtn.rank(title, info_hash)
                        if torrent.fetch:  # Check if the torrent is worth fetching
                            logger.info(f"Best torrent selected: {torrent.data.parsed_title} with rank {torrent.rank}")
                            # Add the best torrent to Real-Debrid
                            result = add_torrent_and_select_files(torrent.infohash, torrent.data.parsed_title, file_idx, media_type)
                            if result.get('success'):
                                return {"success": True, "message": "Torrent added"}
                            else:
                                logger.error("Failed to add torrent to Real-Debrid")
                                return {"success": False, "message": "Failed to add torrent to Real-Debrid"}
                        else:
                            logger.info(f"Torrent {title} does not meet the criteria and will be skipped.")
                            continue
                    except GarbageTorrent:
                        logger.info(f"Torrent {title} is marked as garbage and will be skipped.")
                        continue

        logger.error("No torrents available on Real-Debrid.")
        return {"success": False, "message": "No torrents available on Real-Debrid"}

    except Exception as e:
        logger.error(f"Error processing webhook payload: {e}")
        return {"success": False, "message": str(e)}



# Start processing the queue when the FastAPI server starts
@app.on_event("startup")
async def startup_event():
    # Ask the user for confirmation before running the startup event
    confirmation = input("This may cause duplicates if overseer is not reporting availability properly. Do you want to proceed with the startup event? (y/n): ")
    if confirmation.lower() == 'y':
        start_processing_queue()
    else:
        logger.warning("Startup event skipped by user.")

# Main entry point for running the FastAPI server
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8022)
