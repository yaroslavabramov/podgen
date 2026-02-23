"use client";

import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";

interface TranscriptEntry {
  start_ms: number;
  end_ms: number;
  speaker: string;
  text: string;
}

interface Props {
  episodeId: string;
  transcript: TranscriptEntry[];
}

export function EpisodePlayer({ episodeId, transcript }: Props) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const [playing, setPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);

  const audioUrl = `${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/api/episodes/${episodeId}/audio`;

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;

    const onTimeUpdate = () => setCurrentTime(audio.currentTime * 1000);
    const onLoadedMetadata = () => setDuration(audio.duration * 1000);
    const onEnded = () => setPlaying(false);

    audio.addEventListener("timeupdate", onTimeUpdate);
    audio.addEventListener("loadedmetadata", onLoadedMetadata);
    audio.addEventListener("ended", onEnded);

    return () => {
      audio.removeEventListener("timeupdate", onTimeUpdate);
      audio.removeEventListener("loadedmetadata", onLoadedMetadata);
      audio.removeEventListener("ended", onEnded);
    };
  }, []);

  const togglePlay = () => {
    const audio = audioRef.current;
    if (!audio) return;
    if (playing) {
      audio.pause();
    } else {
      audio.play();
    }
    setPlaying(!playing);
  };

  const seekTo = (ms: number) => {
    const audio = audioRef.current;
    if (audio) {
      audio.currentTime = ms / 1000;
    }
  };

  const formatTime = (ms: number) => {
    const s = Math.floor(ms / 1000);
    const m = Math.floor(s / 60);
    return `${m}:${String(s % 60).padStart(2, "0")}`;
  };

  // Find current transcript entry
  const currentEntry = transcript.find(
    (e) => currentTime >= e.start_ms && currentTime < e.end_ms
  );

  return (
    <Card>
      <CardContent className="py-4 space-y-4">
        <audio ref={audioRef} src={audioUrl} preload="metadata" />

        <div className="flex items-center gap-4">
          <Button onClick={togglePlay} variant="outline" size="icon">
            {playing ? "\u23F8" : "\u25B6"}
          </Button>

          <div className="flex-1">
            <div
              className="h-2 bg-muted rounded-full cursor-pointer relative"
              onClick={(e) => {
                const rect = e.currentTarget.getBoundingClientRect();
                const pct = (e.clientX - rect.left) / rect.width;
                seekTo(pct * duration);
              }}
            >
              <div
                className="h-full bg-primary rounded-full"
                style={{ width: `${duration > 0 ? (currentTime / duration) * 100 : 0}%` }}
              />
            </div>
          </div>

          <span className="text-xs text-muted-foreground whitespace-nowrap">
            {formatTime(currentTime)} / {formatTime(duration)}
          </span>
        </div>

        {/* Synced transcript */}
        <div className="max-h-48 overflow-y-auto space-y-1">
          {transcript.map((entry, i) => {
            const isActive = entry === currentEntry;
            return (
              <button
                key={i}
                onClick={() => seekTo(entry.start_ms)}
                className={`flex gap-2 w-full text-left p-1 rounded text-sm transition-colors ${
                  isActive ? "bg-primary/10" : "hover:bg-muted"
                }`}
              >
                <span className={`font-mono text-xs px-1 rounded ${
                  entry.speaker === "A" ? "text-primary" : "text-accent"
                }`}>
                  {entry.speaker}
                </span>
                <span className={isActive ? "font-medium" : "text-muted-foreground"}>
                  {entry.text}
                </span>
              </button>
            );
          })}
        </div>
      </CardContent>
    </Card>
  );
}
