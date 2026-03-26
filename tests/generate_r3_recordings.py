"""
WellHeard AI — Generate Round 3 Audio Recordings
Uses Cartesia Sonic-3 for SDR (Vicky) and Deepgram for Prospect (Ben).
Generates the best recordings from R3 updated script.
"""
import asyncio
import os
import sys
import wave
import time
from dotenv import load_dotenv
import httpx

load_dotenv("config/.env")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CARTESIA_KEY = os.environ.get("HV_CARTESIA_API_KEY", "")
DEEPGRAM_KEY = os.environ.get("HV_DEEPGRAM_API_KEY", "")

# Cloned voices on Sonic-3
VICKY_VOICE = "734b0cda-9091-4144-9d4d-f33ffc2cc025"
BEN_VOICE = "c1418ac2-d234-478a-9c53-a0e6a5a473e3"

# Round 3 transcripts — best scenarios
R3_QUICK_QUALIFIER = [
    ("sdr", "Hey Luis, how've you been?"),
    ("prospect", "Uh, good I think. Who's this?"),
    ("sdr", "This is Becky with the Benefits Review Team. The reason I'm calling — it looks like a while back you spoke with someone about getting a quote on final expense coverage, you know, for burial or cremation, and I just wanted to follow up on that. I have the last name here as Barragan. Is that right?"),
    ("prospect", "That's right."),
    ("sdr", "Okay, yeah. So here's the thing. A preferred offer for your burial or cremation coverage was marked for you, and for whatever reason it was never claimed. With funeral costs running over nine thousand dollars these days, this is definitely worth looking at — and it actually expires tomorrow. Are you still interested in getting that quote before it expires?"),
    ("prospect", "Yes, I'm interested."),
    ("sdr", "Okay, perfect. So one last thing, people who have a checking or savings account usually get the biggest discounts. Do you have one or the other?"),
    ("prospect", "Yep, I have both."),
    ("sdr", "Okay, great. I have a licensed agent standing by to give you a quote. I'll have them jump on the call ASAP to walk you through all the details."),
    ("sdr", "I'm seeing a preferred discounted offer attached to your profile that reflects the best pricing available today based on your age and health. That pricing window is expiring soon, so we want to make sure the agent reviews it with you before it updates."),
    ("sdr", "The main thing with whole life insurance is making sure you have the right coverage and the right beneficiary so the money goes exactly where you want. The agent will walk you through all of that."),
    ("sdr", "Just so you know, when the agent joins there might be a quick moment of silence as they jump in. As soon as you hear them, just let them know you're there and they'll take great care of you."),
    ("sdr", "Great news — I have the agent on the line now. I'm going to hand you over. It was great talking with you, Luis. You're in good hands."),
    ("prospect", "Okay."),
]

R3_ALREADY_INSURED = [
    ("sdr", "Hey Harold, how've you been?"),
    ("prospect", "Who is this?"),
    ("sdr", "This is Becky with the Benefits Review Team. The reason I'm calling — it looks like a while back you spoke with someone about getting a quote on final expense coverage, you know, for burial or cremation, and I just wanted to follow up on that. I have the last name here as Peterson. Is that right?"),
    ("prospect", "Yeah, that's me."),
    ("sdr", "Okay, yeah. So here's the thing. A preferred offer for your burial or cremation coverage was marked for you, and for whatever reason it was never claimed. With funeral costs running over nine thousand dollars these days, this is definitely worth looking at — and it actually expires tomorrow. Are you still interested in getting that quote before it expires?"),
    ("prospect", "Well, I have life insurance. I have life insurance now. I'm covered with something."),
    ("sdr", "That's great you have something in place! Most folks I talk to do. This is actually about something a little different — it's specifically for final expenses like burial or cremation, so your family isn't pulling from savings or your other policy for that. The agent can show you how it works alongside what you already have. No obligation at all. Are you still interested in getting that quote before it expires?"),
    ("prospect", "Okay. Go ahead."),
    ("sdr", "Okay, perfect. So one last thing, people who have a checking or savings account usually get the biggest discounts. Do you have one or the other?"),
    ("prospect", "I have both."),
    ("sdr", "Great. I have a licensed agent standing by to give you a quote. I'll have them jump on the call ASAP to walk you through all the details."),
    ("sdr", "I'm seeing a preferred discounted offer attached to your profile that reflects the best pricing available today based on your age and health. That pricing window is expiring soon, so we want to make sure the agent reviews it with you before it updates."),
    ("sdr", "The main thing with whole life insurance is making sure you have the right coverage and the right beneficiary so the money goes exactly where you want. The agent will walk you through all of that."),
    ("sdr", "Just so you know, when the agent joins there might be a quick moment of silence as they jump in. As soon as you hear them, just let them know you're there and they'll take great care of you."),
    ("sdr", "Great news — I have the agent on the line now. I'm going to hand you over. It was great talking with you, Harold. You're in good hands."),
]

R3_NOT_INTERESTED = [
    ("sdr", "Hey Frank, how've you been?"),
    ("prospect", "Who's this?"),
    ("sdr", "This is Becky with the Benefits Review Team. The reason I'm calling — it looks like a while back you spoke with someone about getting a quote on final expense coverage, you know, for burial or cremation, and I just wanted to follow up on that. I have the last name here as Davis. Is that right?"),
    ("prospect", "Not interested."),
    ("sdr", "I understand, many people feel that way at first. Just so you know, we're offering free quotes with no obligation to buy, tailored to your budget and needs. Can I ask what concerns you most?"),
    ("prospect", "No. I said I'm not interested. Take me off your list."),
    ("sdr", "I understand you're not interested. I'll make sure to note that and end the call. Have a great day."),
]


async def synthesize_cartesia(text: str, voice_id: str, speed: float = 1.0, emotion: str = "positivity:medium") -> bytes:
    """Synthesize with Cartesia Sonic-3."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.post(
                "https://api.cartesia.ai/tts/bytes",
                headers={
                    "Authorization": f"Bearer {CARTESIA_KEY}",
                    "Cartesia-Version": "2024-06-10",
                    "Content-Type": "application/json"
                },
                json={
                    "model_id": "sonic-3",
                    "transcript": text,
                    "voice": {"mode": "id", "id": voice_id},
                    "output_format": {
                        "container": "raw",
                        "encoding": "pcm_s16le",
                        "sample_rate": 24000,
                    },
                    "language": "en",
                    "generation_config": {
                        "speed": speed,
                        "emotion": emotion,
                        "volume": 1.0,
                    },
                },
            )
            if r.status_code == 200:
                return r.content
            else:
                print(f"    Cartesia error {r.status_code}: {r.text[:100]}")
                return b""
        except Exception as e:
            print(f"    Cartesia exception: {e}")
            return b""


async def generate_recording(turns: list, filename: str, label: str):
    """Generate a WAV recording from turns."""
    print(f"\n  Generating: {label}")
    all_audio = bytearray()
    sample_rate = 24000

    for i, (speaker, text) in enumerate(turns):
        # Natural pause between turns
        if speaker == "sdr":
            # Slightly longer pause before SDR response (thinking time)
            silence_ms = 600 if i > 0 else 200
        else:
            # Prospect responds after a beat
            silence_ms = 400

        silence = b"\x00\x00" * int(sample_rate * silence_ms / 1000)
        all_audio.extend(silence)

        if speaker == "sdr":
            audio = await synthesize_cartesia(
                text, VICKY_VOICE,
                speed=0.95,
                emotion="positivity:medium"
            )
        else:
            audio = await synthesize_cartesia(
                text, BEN_VOICE,
                speed=1.0,
                emotion="neutral"
            )

        if audio:
            all_audio.extend(audio)
            print(f"    [{i+1}/{len(turns)}] {speaker.upper()}: {text[:60]}... ({len(audio)} bytes)")
        else:
            print(f"    [{i+1}/{len(turns)}] {speaker.upper()}: FAILED")

        # Rate limit pause
        await asyncio.sleep(0.8)

    # Write WAV
    try:
        with wave.open(filename, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(bytes(all_audio))
        duration = len(all_audio) / (sample_rate * 2)
        print(f"  Saved: {filename} ({duration:.1f}s)")
    except Exception as e:
        print(f"  WAV error: {e}")


async def main():
    output_dir = "/sessions/gifted-vigilant-bohr/mnt/Crown Academy"

    recordings = [
        (R3_QUICK_QUALIFIER, f"{output_dir}/R3_Quick_Qualifier_90pts.wav", "R3 Quick Qualifier (90/100) — Best happy-path transfer"),
        (R3_ALREADY_INSURED, f"{output_dir}/R3_Already_Insured_90pts.wav", "R3 Already Insured (90/100) — Objection recovery"),
        (R3_NOT_INTERESTED, f"{output_dir}/R3_Not_Interested_90pts.wav", "R3 Not Interested (90/100) — Clean exit"),
    ]

    print("=" * 60)
    print("  WellHeard AI — Round 3 Audio Generation")
    print("  Cartesia Sonic-3 | Cloned Voices")
    print("  Updated script: new greeting + cost anchor + rebuttals")
    print("=" * 60)

    for turns, filename, label in recordings:
        await generate_recording(turns, filename, label)
        # Longer cooldown between recordings
        await asyncio.sleep(2)

    print(f"\n{'='*60}")
    print("  All recordings generated!")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
