"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "use-intl";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import type WaveSurfer from "wavesurfer.js";

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
  const t = useTranslations("episodePlayer");
  const waveformRef = useRef<HTMLDivElement>(null);
  const wavesurferRef = useRef<WaveSurfer | null>(null);
  const [playing, setPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [loadingAudio, setLoadingAudio] = useState(true);
  const [audioError, setAudioError] = useState<string | null>(null);
  const [downloadUrl, setDownloadUrl] = useState<string | null>(null);

  const audioUrl = `${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/api/episodes/${episodeId}/audio`;

  useEffect(() => {
    let cancelled = false;
    let objectUrl: string | null = null;
    setLoadingAudio(true);
    setAudioError(null);
    setDownloadUrl(null);
    setCurrentTime(0);
    setDuration(0);
    setPlaying(false);

    async function setupWaveform() {
      const container = waveformRef.current;
      if (!container) return;
      container.innerHTML = "";

      try {
        const token = localStorage.getItem("token");
        if (!token) throw new Error(t("missingAuth"));

        const response = await fetch(audioUrl, {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (!response.ok) throw new Error(t("failedLoad"));

        const blob = await response.blob();
        if (cancelled) return;

        objectUrl = URL.createObjectURL(blob);
        setDownloadUrl(objectUrl);

        const module = await import("wavesurfer.js");
        if (cancelled || !waveformRef.current) return;

        const rootStyles = getComputedStyle(document.documentElement);
        const waveColor = rootStyles.getPropertyValue("--color-muted-foreground").trim() || "#9ca3af";
        const progressColor = rootStyles.getPropertyValue("--color-primary").trim() || "#6d28d9";

        const ws = module.default.create({
          container: waveformRef.current,
          url: objectUrl,
          waveColor,
          progressColor,
          cursorColor: progressColor,
          height: 80,
          normalize: true,
          barWidth: 2,
          barGap: 2,
          barRadius: 4,
          dragToSeek: true,
        });

        wavesurferRef.current = ws;
        ws.on("ready", () => {
          setDuration(ws.getDuration() * 1000);
          setLoadingAudio(false);
        });
        ws.on("timeupdate", (seconds: number) => {
          setCurrentTime(seconds * 1000);
        });
        ws.on("play", () => setPlaying(true));
        ws.on("pause", () => setPlaying(false));
        ws.on("finish", () => {
          setPlaying(false);
          setCurrentTime(ws.getDuration() * 1000);
        });
      } catch (err) {
        if (!cancelled) {
          const message = err instanceof Error ? err.message : t("failedInit");
          setAudioError(message);
          setLoadingAudio(false);
        }
      }
    }

    setupWaveform();

    return () => {
      cancelled = true;
      const ws = wavesurferRef.current;
      if (ws) {
        ws.destroy();
        wavesurferRef.current = null;
      }
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [audioUrl]);

  const togglePlay = () => {
    const ws = wavesurferRef.current;
    if (!ws || loadingAudio || audioError) return;
    ws.playPause();
    setPlaying(ws.isPlaying());
  };

  const seekTo = (ms: number) => {
    const ws = wavesurferRef.current;
    if (!ws) return;
    ws.setTime(ms / 1000);
    setCurrentTime(ms);
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
        <div className="space-y-2">
          <div ref={waveformRef} className="w-full" />
          {loadingAudio && <p className="text-xs text-muted-foreground">{t("loadingWaveform")}</p>}
          {audioError && <p className="text-xs text-destructive">{audioError}</p>}
        </div>

        <div className="flex items-center gap-4">
          <Button onClick={togglePlay} variant="outline" size="icon" disabled={loadingAudio || !!audioError}>
            {playing ? "\u23F8" : "\u25B6"}
          </Button>
          <span className="text-xs text-muted-foreground whitespace-nowrap">
            {formatTime(currentTime)} / {formatTime(duration)}
          </span>
          {downloadUrl && (
            <a href={downloadUrl} download={`episode-${episodeId}.mp3`}>
              <Button variant="ghost" size="sm">{t("download")}</Button>
            </a>
          )}
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
