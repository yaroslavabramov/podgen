"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { api, type Episode } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { EpisodePlayer } from "@/components/episode-player";

export default function EpisodeDetailPage() {
  const params = useParams();
  const id = params.id as string;
  const [episode, setEpisode] = useState<Episode | null>(null);

  useEffect(() => {
    api.get(`api/episodes/${id}`).json<Episode>().then(setEpisode);
  }, [id]);

  if (!episode) return <div className="p-8 text-muted-foreground">Loading...</div>;

  const minutes = Math.floor(episode.duration_seconds / 60);
  const seconds = episode.duration_seconds % 60;

  return (
    <div className="min-h-screen bg-background">
      <header className="border-b border-border px-6 py-4">
        <h1 className="text-xl font-bold">{episode.title}</h1>
      </header>

      <main className="max-w-4xl mx-auto px-6 py-8 space-y-6">
        <div className="flex items-center gap-3">
          <Badge variant="outline">{episode.language.toUpperCase()}</Badge>
          <Badge variant="secondary">{minutes}m {seconds}s</Badge>
          {episode.quality_metrics && (
            <Badge variant="success">
              {Math.round((episode.quality_metrics.verified_fact_pct || 0))}% verified
            </Badge>
          )}
        </div>

        {episode.audio_file_path && (
          <EpisodePlayer episodeId={episode.id} transcript={episode.transcript_json || []} />
        )}

        {episode.transcript_json && (
          <Card>
            <CardHeader>
              <CardTitle>Transcript</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="space-y-3 max-h-96 overflow-y-auto">
                {episode.transcript_json.map((entry, i) => (
                  <div key={i} className="flex gap-3">
                    <span className={`text-xs font-mono px-2 py-1 rounded ${
                      entry.speaker === "A" ? "bg-primary/10 text-primary" : "bg-accent/10 text-accent"
                    }`}>
                      {entry.speaker}
                    </span>
                    <p className="text-sm flex-1">{entry.text}</p>
                    <span className="text-xs text-muted-foreground whitespace-nowrap">
                      {Math.floor(entry.start_ms / 60000)}:{String(Math.floor((entry.start_ms % 60000) / 1000)).padStart(2, "0")}
                    </span>
                  </div>
                ))}
              </div>
            </CardContent>
          </Card>
        )}

        {episode.shownotes_json && (
          <Card>
            <CardHeader>
              <CardTitle>Show Notes</CardTitle>
            </CardHeader>
            <CardContent>
              <pre className="text-sm whitespace-pre-wrap">{JSON.stringify(episode.shownotes_json, null, 2)}</pre>
            </CardContent>
          </Card>
        )}
      </main>
    </div>
  );
}
