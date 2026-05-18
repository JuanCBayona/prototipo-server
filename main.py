from fastapi import FastAPI
from pydantic import BaseModel
from typing import List

app = FastAPI()

class Song(BaseModel):
    id: int
    title: str
    lyrics: str

@app.get("/api/songs", response_model=List[Song])
def get_songs():
    return [
        {"id": 1, "title": "Hare Krishna Mantra", "lyrics": "Hare Kṛṣṇa Hare Kṛṣṇa..."},
        {"id": 2, "title": "Sri Sri Gurvastakam", "lyrics": "samsara-davanala-lidha-loka..."}
    ]