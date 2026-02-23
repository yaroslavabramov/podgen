"use client";


import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useTranslations } from "use-intl";
import { api, type Episode, type Project } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { LocaleSwitcher } from "@/components/locale-switcher";

function formatDuration(seconds: number): string {
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
  return `${mins}m ${secs}s`;
}

function formatDate(value: string): string {
  return new Date(value).toLocaleString();
}

export default function EpisodesPage() {
  const t = useTranslations("episodesPage");
  const [episodes, setEpisodes] = useState<Episode[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      setLoading(true);
      setError(null);
      try {
        const [episodesResp, projectsResp] = await Promise.all([
          api.get("api/episodes").json<{ items: Episode[] }>(),
          api.get("api/projects").json<{ items: Project[] }>(),
        ]);
        if (!cancelled) {
          setEpisodes(episodesResp.items);
          setProjects(projectsResp.items);
        }
      } catch {
        if (!cancelled) setError(t("loadError"));
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    load();
    return () => {
      cancelled = true;
    };
  }, [t]);

  const projectTitleById = useMemo(() => {
    const map = new Map<string, string>();
    for (const project of projects) map.set(project.id, project.title);
    return map;
  }, [projects]);

  return (
    <div className="min-h-screen bg-background">
      <header className="border-b border-border px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Link href="/projects" className="text-muted-foreground hover:text-foreground">{t("projectsBreadcrumb")}</Link>
          <span className="text-muted-foreground">/</span>
          <h1 className="text-xl font-bold">{t("title")}</h1>
        </div>
        <div className="flex items-center gap-3">
          <LocaleSwitcher />
          <Link href="/projects">
            <Button variant="outline">{t("openProjects")}</Button>
          </Link>
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-6 py-8 space-y-4">
        {loading && <p className="text-muted-foreground">{t("loading")}</p>}
        {error && <p className="text-destructive">{error}</p>}

        {!loading && !error && episodes.length === 0 && (
          <Card>
            <CardContent className="py-8 text-center text-muted-foreground">
              {t("empty")}
            </CardContent>
          </Card>
        )}

        {!loading && !error && episodes.map((episode) => (
          <Card key={episode.id}>
            <CardHeader>
              <CardTitle className="text-lg">
                <Link href={`/episodes/${episode.id}`} className="hover:text-primary">
                  {episode.title}
                </Link>
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant="secondary">{formatDuration(episode.duration_seconds)}</Badge>
                <Badge variant="outline">{episode.language.toUpperCase()}</Badge>
                {episode.quality_metrics && (
                  <Badge variant="success">
                    {t("verifiedPct", { pct: Math.round(episode.quality_metrics.verified_fact_pct || 0) })}
                  </Badge>
                )}
                <Badge variant="secondary">{formatDate(episode.created_at)}</Badge>
              </div>

              <div className="text-sm text-muted-foreground">
                {t("projectLabel")}:{" "}
                <Link href={`/projects/${episode.project_id}`} className="text-primary hover:underline">
                  {projectTitleById.get(episode.project_id) || episode.project_id}
                </Link>
              </div>
            </CardContent>
          </Card>
        ))}
      </main>
    </div>
  );
}
