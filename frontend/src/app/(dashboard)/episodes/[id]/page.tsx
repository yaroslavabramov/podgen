"use client";


import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { useTranslations } from "use-intl";
import { api, type Episode } from "@/lib/api";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { EpisodePlayer } from "@/components/episode-player";
import { ShownotesViewer } from "@/components/shownotes-viewer";
import { LocaleSwitcher } from "@/components/locale-switcher";

export default function EpisodeDetailPage() {
  const t = useTranslations("episodeDetailPage");
  const params = useParams();
  const id = params.id as string;
  const [episode, setEpisode] = useState<Episode | null>(null);

  useEffect(() => {
    api.get(`api/episodes/${id}`).json<Episode>().then(setEpisode);
  }, [id]);

  if (!episode) return <div className="p-8 text-muted-foreground">{t("loading")}</div>;

  const minutes = Math.floor(episode.duration_seconds / 60);
  const seconds = episode.duration_seconds % 60;

  return (
    <div className="min-h-screen bg-background">
      <header className="border-b border-border px-6 py-4 flex items-center justify-between">
        <h1 className="text-xl font-bold">{episode.title}</h1>
        <LocaleSwitcher />
      </header>

      <main className="max-w-4xl mx-auto px-6 py-8 space-y-6">
        <div className="flex items-center gap-3">
          <Badge variant="outline">{episode.language.toUpperCase()}</Badge>
          <Badge variant="secondary">{minutes}m {seconds}s</Badge>
          {episode.quality_metrics && (
            <Badge variant="success">
              {t("verifiedPct", { pct: Math.round((episode.quality_metrics.verified_fact_pct || 0)) })}
            </Badge>
          )}
        </div>

        {episode.audio_file_path && (
          <EpisodePlayer episodeId={episode.id} transcript={episode.transcript_json || []} />
        )}

        {episode.transcript_json && (
          <Card>
            <CardHeader>
              <CardTitle>{t("transcript")}</CardTitle>
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

        <ShownotesViewer shownotes={episode.shownotes_json} />
      </main>
    </div>
  );
}
