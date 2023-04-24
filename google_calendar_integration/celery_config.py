from celery import Celery
import time
import uuid
import json
import os 
from dotenv import load_dotenv
import requests
import re
import google.oauth2.credentials
import google_auth_oauthlib.flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from datetime import datetime, timezone, timedelta
load_dotenv()


celery_app = Celery(
    "google-calendar-integration",
    broker="redis://localhost:6379",
    backend="redis://localhost:6379",
)

celery_app.conf.task_routes = {
    "google-calendar-integration.tasks.*": {"queue": "google-calendar-integration_queue"},
}

@celery_app.task
def start_watch(refresh_token, callback, user_uuid):
    new_uuid = str(uuid.uuid4())
    watch_body = json.dumps({
        "id": new_uuid,
        "token": os.getenv("GOOGLE_TOKEN"),
        "type": "web_hook",
        "address": os.getenv("EVENT_PING_URL"),
        "params": {
            "ttl": "2592000"
        }
    })
    
    try:
        token, token_type = getToken(refresh_token)
    except Exception as e:
        response_request = requests.post(callback, data={"msg": "Failed"})
        return str(e)
    
    watch_url = "https://www.googleapis.com/calendar/v3/calendars/primary/events/watch"
    request_watch = requests.post(watch_url, data=watch_body, headers={'Content-Type': 'x-www-form-url-encoded', 'Authorization': token_type + " " + token})
    response_watch = json.loads(request_watch.text)
    response = {}
    response["uuid"] = user_uuid
    response["google_sync"] = ""
    response["google_channel"] = response_watch.get('id','')
    response["google_expiry"] = response_watch.get('expiration',1)
    
    response_request = requests.post(callback, data=response)
    return response



@celery_app.task
def incoming_ping(channel_id):
        
    user_request = requests.get(os.getenv("TOKEN_URL") + f"{channel_id}/")
    user = json.loads(user_request.text)
    user_uuid = user.get("uuid", "aa")
    sync_token = user.get("sync", "")
    refresh_token = user.get("refresh", "")

    formatted_changed_events, next_sync = fetch_changed_events(sync_token, refresh_token)

    request_callback_ping = requests.post(os.getenv("EVENT_PING_CALLBACK_URL"), json={"channel": channel_id, "next_sync":next_sync, "uuid": user_uuid, "tasks": formatted_changed_events})

    return "OK"


######################
## HELPER FUNCTIONS ##
######################

def getToken(refresh_token):
    body = json.dumps({"grant_type": "refresh_token", "refresh_token": refresh_token, "client_secret": os.getenv("GOOGLE_SECRET"),
                        "client_id": os.getenv("GOOGLE_CLIENT_ID")})
    request = requests.post('https://oauth2.googleapis.com/token', headers={'Content-Type': 'x-www-form-url-encoded'}, data=body)
    response = json.loads(request.text)
    token = response.get("access_token", "")
    token_type = response.get("token_type", "Bearer")
    return token,token_type


# Function to find the link in the event description
def find_link(description):
    zoom_link_pattern = r'/https:\/\/[\w-]*\.?zoom.us\/(j|my)\/[\d\w?=-]+/g'
    meets_link_pattern = r'https:\/\/meet\.google\.com\/[a-z]+-[a-z]+-[a-z]+'
    teams_link_pattern = r'https:\/\/teams\.microsoft\.com\/l\/meetup-join\/[a-zA-Z0-9\/%]+'

    zoom_match = re.search(zoom_link_pattern, description)
    if zoom_match:
        return zoom_match.group(0)

    teams_match = re.search(teams_link_pattern, description)
    if teams_match:
        return teams_match.group(0)
    
    meets_match = re.search(meets_link_pattern, description)
    if meets_match:
        return meets_match.group(0)
    
    return ""


def is_within_time_range(event_start, time_min, time_max):
    start_time_dt = datetime.fromisoformat(event_start.replace('Z', '+00:00')).replace(tzinfo=timezone.utc)
    return time_min <= start_time_dt <= time_max



def fetch_changed_events(sync_token, refresh_token):
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_SECRET")

    # Create the credentials object
    creds = google.oauth2.credentials.Credentials.from_authorized_user_info(
        info={'client_id': client_id, 'client_secret': client_secret, 'refresh_token': refresh_token})

    # Initialize the Calendar API client
    service = build('calendar', 'v3', credentials=creds)

    # Your calendar ID
    calendar_id = 'primary'

    try:
        # Get the updated events using sync token

        tasks = []
        page_token = None
        now = datetime.now(timezone.utc)
        time_min = now
        time_max = now + timedelta(days=20)


        for _ in range(100):
            # Get the updated events using sync token
            now = datetime.now(timezone.utc)
            
            
            events_results = service.events().list(calendarId=calendar_id, syncToken=sync_token,
                                                   showDeleted=True, pageToken=page_token, singleEvents=True).execute()

            for event in events_results['items']:
                start_time = event['start'].get('dateTime', event['start'].get('date'))
                if is_within_time_range(start_time, time_min, time_max):
                    if 'status' in event and event['status'] == 'cancelled':
                        tasks.append({'DELETE': [event['id']]})
                    else:
                        link = find_link(json.dumps(event))
                        tasks.append({'UPDATE': [event['id'],
                                                link,
                                                event['start'].get('dateTime', event['start'].get('date')),
                                                event.get('summary', ''),
                                                [attendee['email'] for attendee in event.get('attendees', [])]]})

            # If there is a nextPageToken, update the sync_token variable and continue the loop
            if 'nextPageToken' in events_results:
                page_token = events_results['nextPageToken']
            else:
                break



        return tasks, events_results['nextSyncToken']
    except HttpError as error:
        print(f'An error occurred: {error}')
        return None, None
        
        
        
        

