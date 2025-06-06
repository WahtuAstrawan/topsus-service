from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from gtts import gTTS
from dotenv import load_dotenv
from zyphra import ZyphraClient
import os
import whisper
import datetime
import base64
import requests
import tempfile
from moviepy import VideoFileClip, concatenate_videoclips, AudioFileClip
from google import genai

# Load environment variables
load_dotenv()
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

app = FastAPI()
gemini_client = genai.Client(api_key=GEMINI_API_KEY)
model = whisper.load_model("base")
zyphraClient = ZyphraClient(api_key=os.getenv("API_KEY"))

# Create directories if they don't exist
INPUT_DIR = "input"
OUTPUT_DIR = "output"
os.makedirs(INPUT_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)


class EmotionWeights(BaseModel):
    happiness: float = Field(0.6, description="Default: 0.6")
    sadness: float = Field(0.05, description="Default: 0.05")
    disgust: float = Field(0.05, description="Default: 0.05")
    fear: float = Field(0.05, description="Default: 0.05")
    surprise: float = Field(0.05, description="Default: 0.05")
    anger: float = Field(0.05, description="Default: 0.05")
    other: float = Field(0.5, description="Default: 0.5")
    neutral: float = Field(0.6, description="Default: 0.6")


class TTSRequest(BaseModel):
    text: str
    lang: str = "id"
    slow: bool = False


class TTSZyphra(BaseModel):
    text: str
    speaking_rate: int = 15
    model: str = "zonos-v0.1-transformer"
    fmax: int = 22050
    pitch_std: float = 45.0
    emotion: EmotionWeights
    language_iso_code: str = "id"
    mime_type: str = "audio/mpeg"


class CloneTTSRequest(BaseModel):
    text: str


class GenerateVideoStoryRequest(BaseModel):
    story: str
    lang: str = "id"
    slow: bool = False
    clip_duration: int = 3
    min_width: int = 1280
    min_height: int = 720
    video_type: str = "all"


def extract_keywords(text: str):
    """
    Extract the most relevant keyword phrases from text using Gemini AI.
    Returns a list of keyword phrases
    """
    try:
        prompt = (
            "Dari teks berikut, ekstrak frasa kata kunci yang paling relevan "
            "untuk digunakan dalam pencarian video ilustratif. "
            "Gunakan frasa pendek (1-5 kata) yang menggambarkan adegan atau momen penting secara urut."
            f"Teks:\n\"{text}\"\n\n"
            "Contoh input:pada hari minggu, saat andi bersepeda ke sekolahnya dia jatuh di jalan dan kakinya luka"
            "Contoh output:hari minggu cerah,seorang anak,bersepeda ke sekolah,jatuh di jalan,kaki terluka"
            "Output hanya dalam bentuk plaintext dengan format csv dan buat dalam Bahasa Inggris (tanpa penjelasan tambahan)"
        )
        response = gemini_client.models.generate_content(
            model="gemini-2.0-flash", contents=prompt
        )
        print("OUTPUT GEMINI:")
        print(response.text)
        keyword_phrases = [phrase.strip() for phrase in response.text.strip().split(',')]
        print("OUTPUT KEYWORD:")
        print(keyword_phrases)
        return keyword_phrases
    except Exception as e:
        print(f"Error extracting keywords with Gemini: {e}")
        # Fallback keyword extraction
        words = text.lower().split()
        return [w for w in words if len(w) > 3][:]


def search_pixabay_video(keyword, min_width, min_height, video_type):
    """Search for a video on Pixabay API based on keyword"""
    url = f"https://pixabay.com/api/videos/?key={PIXABAY_API_KEY}&q={keyword}&min_width={min_width}&min_height={min_height}&video_type={video_type}"

    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()  # Raise exception for HTTP errors
        data = response.json()

        hits = data.get("hits", [])
        if not hits:
            return None

        # Get the first hit's medium video URL or small if not available
        for hit in hits:
            if "videos" in hit:
                if "medium" in hit["videos"]:
                    return hit["videos"]["medium"]["url"]
                elif "small" in hit["videos"]:
                    return hit["videos"]["small"]["url"]

        return None
    except (requests.RequestException, ValueError) as e:
        print(f"Error searching Pixabay: {e}")
        return None

@app.post("/generate/video")
async def generate_video_by_story(request: GenerateVideoStoryRequest):
    """
    Generate a video from a story by:
    1. Extracting keywords from the story
    2. Searching Pixabay for videos related to each keyword
    3. Downloading and concatenating the videos
    4. Adding narration audio using TTS
    """
    story = request.story

    # Step 1: Extract keywords from the story
    keywords = extract_keywords(story)
    if not keywords:
        return {"error": "Could not extract keywords from the story."}

    # Create a temporary directory for downloaded videos
    temp_dir = tempfile.mkdtemp()

    # Step 2: Search and download videos for each keyword
    video_paths = []
    for i, keyword in enumerate(keywords):
        video_url = search_pixabay_video(keyword, request.min_width, request.min_height, request.video_type)
        if not video_url:
            print(f"No video found for keyword: {keyword}")
            continue

        try:
            # Download video
            video_response = requests.get(video_url, timeout=30)
            video_response.raise_for_status()

            # Save to temporary file
            tmp_video_path = os.path.join(temp_dir, f"video_{i}_{keyword}.mp4")
            with open(tmp_video_path, 'wb') as f:
                f.write(video_response.content)

            # Add to list of video paths
            video_paths.append(tmp_video_path)
            print(f"Downloaded video for keyword: {keyword}")
        except Exception as e:
            print(f"Error downloading video for keyword '{keyword}': {e}")

    if not video_paths:
        return {"error": "Could not download any videos for the keywords."}

    # Step 3: Load video clips and set duration
    video_clips = []
    for path in video_paths:
        try:
            clip = VideoFileClip(path)
            # Subclip to requested duration (or less if video is shorter)
            clip_duration = min(request.clip_duration, clip.duration)
            clip = clip.subclipped(0, clip_duration)
            video_clips.append(clip)
        except Exception as e:
            print(f"Error loading video clip {path}: {e}")

    if not video_clips:
        return {"error": "Could not process any video clips."}

    # Step 4: Concatenate video clips
    try:
        final_video = concatenate_videoclips(video_clips, method="compose")
    except Exception as e:
        return {"error": f"Error concatenating video clips: {e}"}

    # Step 5: Generate narration audio using TTS
    tts_path = os.path.join(temp_dir, "narration.mp3")
    try:
        tts = gTTS(text=story, lang=request.lang, slow=request.slow)
        tts.save(tts_path)
        narration_audio = AudioFileClip(tts_path)
    except Exception as e:
        return {"error": f"Error generating narration audio: {e}"}

    # Step 6: Add narration to the video
    try:
        # If narration is longer than video, extend video by looping
        if narration_audio.duration > final_video.duration:
            # Calculate how many times to loop the video
            n_loops = int(narration_audio.duration / final_video.duration) + 1
            # Loop the video
            final_video = concatenate_videoclips([final_video] * n_loops).subclipped(0, narration_audio.duration)

        # Set audio to the video
        final_video = final_video.with_audio(narration_audio)
    except Exception as e:
        return {"error": f"Error setting audio to video: {e}"}

    # Step 7: Save the final video
    timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    output_video_path = os.path.join(OUTPUT_DIR, f"video_story_{timestamp}.mp4")

    try:
        final_video.write_videofile(
            output_video_path,
            codec="libx264",
            audio_codec="aac",
            fps=24,
            threads=4,
            preset="ultrafast"  # Use faster preset for quicker encoding
        )
    except Exception as e:
        return {"error": f"Error writing output video file: {e}"}
    finally:
        # Clean up clips to release file handles
        for clip in video_clips:
            clip.close()
        final_video.close()
        narration_audio.close()

    return FileResponse(
        output_video_path,
        media_type="video/mp4",
        filename=os.path.basename(output_video_path)
    )

@app.get('/')
def root():
    return {"message": "Hello, World!"}


@app.post('/tts')
async def text_to_speech(request: TTSRequest):
    timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    output_filename = os.path.join(OUTPUT_DIR, f"tts_{timestamp}.mp3")

    tts = gTTS(text=request.text, lang=request.lang, slow=request.slow)
    tts.save(output_filename)

    return FileResponse(output_filename, media_type="audio/mpeg", filename=os.path.basename(output_filename))


@app.post("/stt")
async def speech_to_text():
    input_audio_path = os.path.join(INPUT_DIR, "speech.mp3")
    timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    output_text_filename = os.path.join(OUTPUT_DIR, f"stt_{timestamp}.txt")

    result = model.transcribe(input_audio_path)

    with open(output_text_filename, "w") as text_file:
        text_file.write(result["text"])

    return {"text": result["text"]}


@app.post("/sts")
async def speech_to_speech(lang: str = "id", slow: bool = False):
    input_audio_path = os.path.join(INPUT_DIR, "speech.mp3")
    timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    output_filename = os.path.join(OUTPUT_DIR, f"sts_{timestamp}.mp3")

    result = model.transcribe(input_audio_path)
    transcribed_text = result["text"]

    tts = gTTS(text=transcribed_text, lang=lang, slow=slow)
    tts.save(output_filename)

    return FileResponse(output_filename, media_type="audio/mpeg", filename=os.path.basename(output_filename))


@app.post("/tts/zyphra")
async def text_to_speech_zyphra(request: TTSZyphra):
    timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    output_filename = os.path.join(OUTPUT_DIR, f"tts_zyphra_{timestamp}.mp3")

    output_path = zyphraClient.audio.speech.create(
        text=request.text,
        speaking_rate=request.speaking_rate,
        fmax=request.fmax,
        pitch_std=request.pitch_std,
        emotion=request.emotion.model_dump(),
        language_iso_code=request.language_iso_code,
        mime_type=request.mime_type,
        model=request.model,
        output_path=str(output_filename)
    )

    return FileResponse(output_path, media_type="audio/mpeg", filename=os.path.basename(output_filename))


@app.post("/tts/clone")
async def text_to_speech_clone(request: CloneTTSRequest):
    input_audio_path = os.path.join(INPUT_DIR, "clone_test.wav")

    with open(input_audio_path, "rb") as f:
        audio_base64 = base64.b64encode(f.read()).decode("utf-8")

    timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    output_filename = os.path.join(OUTPUT_DIR, f"clone_{timestamp}.mp3")

    output_path = zyphraClient.audio.speech.create(
        text=request.text,
        speaker_audio=audio_base64,
        speaking_rate=15,
        model="zonos-v0.1-transformer",
        output_path=str(output_filename)
    )

    return FileResponse(output_path, media_type="audio/mpeg", filename=os.path.basename(output_filename))