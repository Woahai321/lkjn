import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from loguru import logger
from bs4 import BeautifulSoup
import re
import os
import mimetypes
from typing import Optional, List, Any, Dict, Literal
from ratelimit import limits, sleep_and_retry
from dotenv import load_dotenv
import RTN
from RTN.models import SettingsModel, CustomRank, DefaultRanking
from RTN.exceptions import GarbageTorrent
from queue import Queue
import threading
from settings import rtn, settings

# Constants for APIs
REAL_DEBRID_API_BASE_URL = "https://api.real-debrid.com/rest/1.0"
TORRENTIO_API_URL = "https://torrentio.strem.fun/qualityfilter=scr,cam/stream/movie/{imdb_id}.json"
RD_INSTANT_AVAILABILITY_URL = f"{REAL_DEBRID_API_BASE_URL}/torrents/instantAvailability/{{hash}}"
RD_ADD_TORRENT_URL = f"{REAL_DEBRID_API_BASE_URL}/torrents/addMagnet"

# Rate limits for Real-Debrid API
MAX_CALLS_PER_MINUTE = 60

# Load environment variables from .env file
load_dotenv()

# Load Real-Debrid & TRAKT API key from environment variables
RD_API_KEY = os.getenv('RD_API_KEY')
TRAKT_API_KEY = os.getenv('TRAKT_API_KEY')

# Initialize a persistent session with retry logic
session = requests.Session()
retries = Retry(total=5, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
session.mount('https://', HTTPAdapter(max_retries=retries))

# Set default headers for the session
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3"
})

# Overseerr API settings CHANGE BEFORE RELEASE
OVERSEERR_BASE = os.getenv('OVERSEERR_BASE')
OVERSEERR_API_BASE_URL = f"{OVERSEERR_BASE}/api/v1"
OVERSEERR_API_KEY = os.getenv('OVERSEERR_API_KEY')

# Initialize the queue
request_queue = Queue()

# Function to process requests from the queue
def process_request_queue():
    while True:
        try:
            request = request_queue.get()
            if request is None:
                break
            process_overseerr_request(request)
            request_queue.task_done()
        except Exception as e:
            logger.error(f"Error processing request: {e}")
            request_queue.task_done()  # Ensure the queue is not blocked

# Function to fetch media requests from Overseerr
def get_overseerr_media_requests() -> list[dict]:
    url = f"{OVERSEERR_API_BASE_URL}/request?take=1000&filter=approved&sort=added"
    headers = {
        "X-Api-Key": OVERSEERR_API_KEY
    }
    response = session.get(url, headers=headers)
    
    if response.status_code != 200:
        logger.error(f"Failed to fetch requests from Overseerr: {response.status_code}")
        return []
    
    data = response.json()
    if not data.get('results'):
        return []
    
    # Filter requests that are in processing state (status 3)
    processing_requests = [item for item in data['results'] if item['status'] == 2 and item['media']['status'] == 3]
    return processing_requests

# Function to process a single Overseerr request
def process_overseerr_request(request: dict):
    try:
        media_id = request['media']['id']
        tmdb_id = request['media']['tmdbId']
        media_type = request['media']['mediaType']
        logger.info(f"Processing Overseerr request for media ID: {media_id}, tmdbId: {tmdb_id}")
        
        # Fetch IMDb ID using Trakt API
        imdb_id = get_imdb_id_from_trakt(tmdb_id, media_type)  # Pass media_type here
        if not imdb_id:
            logger.error("IMDb ID not found")
            return
        
        logger.info(f"IMDb ID found: {imdb_id}")
        
        # Query Torrentio API to get torrents
        torrentio_results = query_torrentio(imdb_id, media_type)  # Pass media_type here
        if not torrentio_results or not torrentio_results.get('streams'):
            logger.error("No torrents found on Torrentio")
            return
        
        # Check Real-Debrid availability and rank torrents
        ranked_torrents = []
        checked_hashes = set()
        garbage_count = 0
        max_hashes_to_check = 5
        
        for stream in torrentio_results['streams']:
            info_hash = stream.get('infoHash')
            title = stream.get('title')
            
            if info_hash and title and info_hash not in checked_hashes:
                checked_hashes.add(info_hash)
                rd_availability = check_rd_availability(info_hash)
                if rd_availability:
                    try:
                        torrent = rtn.rank(title, info_hash)
                        if torrent.fetch:
                            ranked_torrents.append(torrent)
                        else:
                            garbage_count += 1
                    except GarbageTorrent:
                        logger.info(f"Torrent {title} is marked as garbage and will be skipped.")
                        garbage_count += 1
                else:
                    logger.info(f"Torrent with hash {info_hash} is not available on Real-Debrid.")
                
                # If we have checked 5 hashes and all are garbage, break out
                if len(checked_hashes) >= max_hashes_to_check and garbage_count == max_hashes_to_check:
                    logger.info("All checked torrents are garbage. No need to check more.")
                    break
        
        # Sort torrents by rank in descending order
        sorted_torrents = sorted(ranked_torrents, key=lambda x: x.rank, reverse=True)
        
        # Limit to the top 5 torrents
        top_torrents = sorted_torrents[:5]
        
        if not top_torrents:
            logger.error("No valid torrents found after ranking")
            return
        
        # Proceed with the top ranked torrent
        best_torrent = top_torrents[0]
        logger.info(f"Best torrent selected: {best_torrent.data.parsed_title} with rank {best_torrent.rank}")
        
        # Add the best torrent to Real-Debrid
        result = add_torrent_and_select_files(best_torrent.infohash, best_torrent.data.parsed_title, "1", media_type)  # Pass media_type here
        if result.get('success'):
            # Mark the request as completed in Overseerr
            mark_completed(request['media']['id'])
        else:
            logger.error("Failed to add torrent to Real-Debrid")
    except Exception as e:
        logger.error(f"Error processing Overseerr request: {e}")


def mark_completed(media_id: int) -> bool:
    """Mark item as completed in overseerr"""
    url = f"{OVERSEERR_BASE}/api/v1/media/{media_id}/available"
    headers = {
        "X-Api-Key": OVERSEERR_API_KEY,
        "Content-Type": "application/json"
    }
    data = {"is4k": False}
    try:
        response = session.post(url, headers=headers, json=data)
        if response.status_code == 200:
            logger.info(f"Marked media {media_id} as completed in overseerr")
            return True
        else:
            logger.error(f"Failed to mark media as completed in overseerr with id {media_id}: Status code {response.status_code}")
            return False
    except Exception as e:
        logger.error(f"Failed to mark media as completed in overseerr with id {media_id}: {str(e)}")
        return False

# Function to start processing the queue
def start_processing_queue():
    # Fetch media requests from Overseerr
    overseerr_requests = get_overseerr_media_requests()
    if not overseerr_requests:
        logger.warning("No requests fetched from Overseerr.")
    for request in overseerr_requests:
        request_queue.put(request)
    
    # Start worker threads
    num_threads = 5
    for _ in range(num_threads):
        threading.Thread(target=process_request_queue, daemon=True).start()
    
    # Wait for the queue to be processed
    request_queue.join()

# Step 2: Search Trakt for the IMDb ID
def get_imdb_id_from_trakt(tmdb_id: int, media_type: str) -> Optional[str]:
    """
    Fetch the IMDb ID using the Trakt API based on the TMDb ID.
    """
    if media_type == "tv":
        url = f"https://api.trakt.tv/search/tmdb/{tmdb_id}?type=show"
    else:
        url = f"https://api.trakt.tv/search/tmdb/{tmdb_id}?type=movie"
    
    headers = {
        "Content-type": "application/json",
        "trakt-api-key": TRAKT_API_KEY,
        "trakt-api-version": "2"
    }
    
    for attempt in range(5):  # Retry up to 5 times
        try:
            response = session.get(url, headers=headers, timeout=10)  # Add timeout here
            
            if response.status_code == 200:
                data = response.json()
                if data and isinstance(data, list) and data:
                    # Corrected logic to extract IMDb ID
                    if media_type == "tv":
                        return data[0]['show']['ids']['imdb']
                    else:
                        return data[0]['movie']['ids']['imdb']
                else:
                    logger.error("IMDb ID not found in Trakt API response.")
                    return None
            else:
                logger.error(f"Trakt API request failed with status code {response.status_code}")
                return None
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching IMDb ID from Trakt API (attempt {attempt + 1}): {e}")
            if attempt == 4:  # Last attempt
                return None




# Step 3: Query Torrentio API to get available torrents
@sleep_and_retry
@limits(calls=MAX_CALLS_PER_MINUTE, period=60)
def query_torrentio(imdb_id: str, media_type: str) -> Optional[Dict[str, Any]]:
    if media_type == "tv":
        url = f"https://torrentio.strem.fun/qualityfilter=scr,cam/stream/series/{imdb_id}:1:1.json"
    else:
        url = f"https://torrentio.strem.fun/qualityfilter=scr,cam/stream/movie/{imdb_id}.json"
    
    for attempt in range(5):  # Retry up to 5 times
        try:
            response = session.get(url, timeout=10)  # Add timeout here
            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"Torrentio API failed with status code {response.status_code}")
                return None
        except requests.exceptions.RequestException as e:
            logger.error(f"Error querying Torrentio API (attempt {attempt + 1}): {e}")
            if attempt == 4:  # Last attempt
                return None
        
# Step 4: Check torrent availability on Real-Debrid
def get_instant_availability(hashes: list[str]) -> dict[str, dict[int, dict[str, int]]]:
    """
    Get the instant availability of hash(es). Normalizes the output into a dict.

    Example:
        Input: ["2f5a5ccb7dc32b7f7d7b150dd6efbce87d2fc371", "10CE69DFFB064E887E8833E7754F71AA7532C997"]
        Output:
        {
            "2f5a5ccb7dc32b7f7d7b150dd6efbce87d2fc371": {  # Cached
                1: {
                    "filename": "Mortal.Kombat.2021.1080p.WEBRip.x264.AAC5.1-[YTS.MX].mp4",
                    "filesize": 2176618694
                }
            },
            "10CE69DFFB064E887E8833E7754F71AA7532C997": {}, # Non-cached
        }
    """
    hashes_str = "/".join(hashes)
    url = f"{REAL_DEBRID_API_BASE_URL}/torrents/instantAvailability/{hashes_str}"
    headers = {
        'Authorization': f'Bearer {RD_API_KEY}'
    }
    response = session.get(url, headers=headers)
    
    if response.status_code != 200:
        logger.error(f"Real-Debrid instant availability failed with status code {response.status_code}")
        return {hash: {} for hash in hashes}

    data = response.json()
    
    # Ensure the response is a dictionary and not a list
    if isinstance(data, list):
        logger.error("Unexpected response format: received a list instead of a dictionary.")
        return {hash: {} for hash in hashes}
    
    results = {}
    for hash, values in data.items():
        if "rd" in values and values["rd"]:
            result = {int(k): {"filename": v["filename"], "filesize": v["filesize"]} 
                      for container in values["rd"] for k, v in container.items()}
            results[hash] = result
        else:
            results[hash] = {}
    return results

# Step 4.5: Check torrent availability on Real-Debrid
@sleep_and_retry
@limits(calls=MAX_CALLS_PER_MINUTE, period=60)
def check_rd_availability(info_hash: str) -> Optional[Dict[str, Any]]:
    """
    Check the instant availability of a torrent hash on Real-Debrid.
    """
    availability = get_instant_availability([info_hash])
    if isinstance(availability, dict) and info_hash in availability:
        if availability[info_hash]:
            logger.info(f"Torrent with hash {info_hash} is available on Real-Debrid.")
            return availability[info_hash]
        else:
            logger.info(f"Torrent with hash {info_hash} is not available on Real-Debrid.")
            return None
    else:
        logger.error(f"Unexpected response format from Real-Debrid for hash {info_hash}")
        return None

# Step 5: Add torrent to Real-Debrid and select specific files
# Step 5: Add torrent to Real-Debrid and select specific files
# Step 5: Add torrent to Real-Debrid and select specific files
@sleep_and_retry
@limits(calls=MAX_CALLS_PER_MINUTE, period=60)
def add_torrent_and_select_files(info_hash: str, torrent_name: str, file_idx: int, media_type: str) -> Optional[Dict[str, Any]]:
    """
    Add a torrent to Real-Debrid and select specific files.
    
    :param info_hash: The info hash of the torrent.
    :param torrent_name: The name of the torrent.
    :param file_idx: The index of the file to select (0-based index).
    :param media_type: The type of media (movie or tv).
    :return: The response from the Real-Debrid API if successful, None otherwise.
    """
    url = RD_ADD_TORRENT_URL
    headers = {
        'Authorization': f'Bearer {RD_API_KEY}'
    }
    data = {
        'magnet': f"magnet:?xt=urn:btih:{info_hash}&dn={torrent_name}"
    }
    response = session.post(url, headers=headers, data=data)
    
    if response.status_code == 201:
        rd_response = response.json()
        torrent_id = rd_response.get('id')
        
        if torrent_id:
            logger.info(f"Torrent added to Real-Debrid successfully: {info_hash}")
            
            # Step 6: Select specific files in the torrent
            if select_files_in_rd(torrent_id, file_idx, media_type):
                return {"success": True, "message": f"Torrent added and files selected.", "torrent_id": torrent_id}
            else:
                return {"success": False, "message": "Failed to select files in the torrent."}
        else:
            logger.error("Torrent ID not found in Real-Debrid response.")
            return {"success": False, "message": "Torrent added, but torrent ID not found."}
    else:
        logger.error(f"Failed to add torrent to Real-Debrid with status code {response.status_code}")
        return None




# Step 6: Select specific files from the torrent in Real-Debrid
# Step 6: Select specific files from the torrent in Real-Debrid
@sleep_and_retry
@limits(calls=MAX_CALLS_PER_MINUTE, period=60)
def select_files_in_rd(torrent_id: str, file_idx: int, media_type: str) -> bool:
    """
    Select specific files in a Real-Debrid torrent using the torrent ID.
    
    :param torrent_id: The ID of the torrent in Real-Debrid.
    :param file_idx: The index of the file to be selected (0-based index).
    :param media_type: The type of media (movie or tv).
    :return: True if the files were successfully selected, False otherwise.
    """
    url = f"{REAL_DEBRID_API_BASE_URL}/torrents/selectFiles/{torrent_id}"
    headers = {
        'Authorization': f'Bearer {RD_API_KEY}'
    }
    
    # Fetch the list of files in the torrent
    files_url = f"{REAL_DEBRID_API_BASE_URL}/torrents/info/{torrent_id}"
    files_response = session.get(files_url, headers=headers)
    
    if files_response.status_code != 200:
        logger.error(f"Failed to fetch files for torrent ID: {torrent_id}. Status code: {files_response.status_code}")
        return False
    
    files_data = files_response.json()
    files = files_data.get('files', [])
    
    # Ensure the file index is within the range of available files
    if file_idx < 0 or file_idx >= len(files):
        logger.error(f"File index {file_idx} is out of range for torrent ID: {torrent_id}")
        return False
    
    if media_type == "movie":
        # For movies, select the specific file based on file_idx
        selected_file_id = str(files[file_idx]['id'])
        data = {
            'files': selected_file_id  # Select the appropriate file
        }
    elif media_type == "tv":
        # For TV shows, select all playable files
        playable_files = [str(file['id']) for file in files if mimetypes.guess_type(file.get('path', ''))[0] and mimetypes.guess_type(file.get('path', ''))[0].startswith('video')]
        if not playable_files:
            logger.error(f"No playable files found in torrent ID: {torrent_id}")
            return False
        data = {
            'files': ",".join(playable_files)  # Select all playable files
        }
    else:
        logger.error(f"Unknown media type: {media_type}")
        return False
    
    response = session.post(url, headers=headers, data=data)
    
    if response.status_code == 204:
        logger.info(f"Files successfully selected for torrent ID: {torrent_id}")
        return True
    else:
        logger.error(f"Failed to select files for torrent ID: {torrent_id}. Status code: {response.status_code}")
        return False



