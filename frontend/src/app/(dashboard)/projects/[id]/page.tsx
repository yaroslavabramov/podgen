"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { useTranslations } from "use-intl";
import { useProjectStore } from "@/stores/project-store";
import { api, type Episode } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { SourceUploader } from "@/components/source-uploader";
import { ShowBibleEditor } from "@/components/show-bible-editor";
import { LocaleSwitcher } from "@/components/locale-switcher";

export default function ProjectDetailPage() {
  const t = useTranslations("projectDetailPage");
  const params = useParams();
  const id = params.id as string;
  const { currentProject, sources, fetchProject, fetchSources, deleteSource, reingestSource, updateProjectLanguage } = useProjectStore();
  const [activeTab, setActiveTab] = useState<"sources" | "bible" | "episodes">("sources");
  const [episodes, setEpisodes] = useState<Episode[]>([]);
  const [episodesLoading, setEpisodesLoading] = useState(false);
  const [projectLanguage, setProjectLanguage] = useState<"en" | "ru">("en");
  const [savingLanguage, setSavingLanguage] = useState(false);
  const [languageError, setLanguageError] = useState<string | null>(null);
  const [deletingSourceId, setDeletingSourceId] = useState<string | null>(null);
  const [deleteSourceError, setDeleteSourceError] = useState<string | null>(null);
  const [reingestingSourceId, setReingestingSourceId] = useState<string | null>(null);
  const [reingestSourceError, setReingestSourceError] = useState<string | null>(null);

  useEffect(() => {
    fetchProject(id);
    fetchSources(id);
  }, [id, fetchProject, fetchSources]);

  useEffect(() => {
    let cancelled = false;

    async function loadEpisodes() {
      setEpisodesLoading(true);
      try {
        const data = await api.get("api/episodes", { searchParams: { project_id: id } }).json<{ items: Episode[] }>();
        if (!cancelled) setEpisodes(data.items);
      } catch {
        if (!cancelled) setEpisodes([]);
      } finally {
        if (!cancelled) setEpisodesLoading(false);
      }
    }

    loadEpisodes();
    return () => {
      cancelled = true;
    };
  }, [id]);

  useEffect(() => {
    if (!currentProject || currentProject.id !== id) return;
    setProjectLanguage(currentProject.language === "ru" ? "ru" : "en");
  }, [currentProject, id]);

  const handleSaveLanguage = async () => {
    if (!currentProject || currentProject.id !== id) return;
    if (projectLanguage === currentProject.language) return;

    setSavingLanguage(true);
    setLanguageError(null);
    try {
      await updateProjectLanguage(id, projectLanguage);
    } catch {
      setLanguageError(t("languageSaveFailed"));
    } finally {
      setSavingLanguage(false);
    }
  };

  const formatDuration = (seconds: number) => {
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins}m ${secs}s`;
  };

  const handleDeleteSource = async (sourceId: string, sourceLabel: string) => {
    const confirmed = window.confirm(t("deleteSourceConfirm", { title: sourceLabel }));
    if (!confirmed) return;

    setDeleteSourceError(null);
    setDeletingSourceId(sourceId);
    try {
      await deleteSource(sourceId);
    } catch {
      setDeleteSourceError(t("deleteSourceFailed"));
    } finally {
      setDeletingSourceId(null);
    }
  };

  const handleReingestSource = async (sourceId: string) => {
    setReingestSourceError(null);
    setReingestingSourceId(sourceId);
    try {
      await reingestSource(sourceId);
    } catch {
      setReingestSourceError(t("reingestSourceFailed"));
    } finally {
      setReingestingSourceId(null);
    }
  };

  if (!currentProject || currentProject.id !== id) return <div className="p-8 text-muted-foreground">{t("loading")}</div>;

  return (
    <div className="min-h-screen bg-background">
      <header className="border-b border-border px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-4">
          <Link href="/projects" className="text-muted-foreground hover:text-foreground">{t("projectsBreadcrumb")}</Link>
          <span className="text-muted-foreground">/</span>
          <h1 className="text-xl font-bold">{currentProject.title}</h1>
        </div>
        <div className="flex items-center gap-3">
          <LocaleSwitcher />
          <Link href={`/projects/${id}/generate`}>
            <Button>{t("generatePodcast")}</Button>
          </Link>
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-6 py-8">
        <section className="mb-6 rounded-lg border border-border p-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
            <div>
              <p className="text-sm font-medium">{t("projectLanguageLabel")}</p>
              <p className="text-sm text-muted-foreground">{t("projectLanguageHelp")}</p>
            </div>
            <div className="flex items-center gap-2">
              <select
                value={projectLanguage}
                onChange={(e) => setProjectLanguage(e.target.value === "ru" ? "ru" : "en")}
                disabled={savingLanguage}
                className="rounded-md border border-border px-3 py-2 text-sm bg-background"
              >
                <option value="en">{t("languageEnglish")}</option>
                <option value="ru">{t("languageRussian")}</option>
              </select>
              <Button
                variant="outline"
                onClick={handleSaveLanguage}
                disabled={savingLanguage || projectLanguage === currentProject.language}
              >
                {savingLanguage ? t("savingLanguage") : t("saveLanguage")}
              </Button>
            </div>
          </div>
          {languageError && <p className="mt-2 text-sm text-destructive">{languageError}</p>}
        </section>

        <div className="flex gap-1 mb-6 border-b border-border">
          {(["sources", "bible", "episodes"] as const).map((tab) => (
            <button
              key={tab}
              onClick={() => setActiveTab(tab)}
              className={`px-4 py-2 text-sm font-medium border-b-2 transition-colors ${
                activeTab === tab
                  ? "border-primary text-primary"
                  : "border-transparent text-muted-foreground hover:text-foreground"
              }`}
            >
              {tab === "sources" ? t("tabSources") : tab === "bible" ? t("tabBible") : t("tabEpisodes")}
            </button>
          ))}
        </div>

        {activeTab === "sources" && (
          <div className="space-y-6">
            <SourceUploader projectId={id} onUploaded={() => fetchSources(id)} />
            {deleteSourceError && <p className="text-sm text-destructive">{deleteSourceError}</p>}
            {reingestSourceError && <p className="text-sm text-destructive">{reingestSourceError}</p>}

            <div className="space-y-3">
              {sources.map((source) => (
                <Card key={source.id}>
                  <CardContent className="flex items-center justify-between py-4">
                    <div>
                      <p className="font-medium">{source.title || source.original_url || t("sourceUntitled")}</p>
                      <p className="text-sm text-muted-foreground">{source.source_type.toUpperCase()}</p>
                    </div>
                    <div className="flex items-center gap-2">
                      <Badge
                        variant={
                          source.ingestion_status === "completed" ? "success" :
                          source.ingestion_status === "failed" ? "destructive" :
                          source.ingestion_status === "processing" ? "warning" : "secondary"
                        }
                      >
                        {source.ingestion_status}
                      </Badge>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => handleReingestSource(source.id)}
                        disabled={
                          reingestingSourceId === source.id ||
                          deletingSourceId === source.id ||
                          source.ingestion_status === "pending" ||
                          source.ingestion_status === "processing" ||
                          source.ingestion_status === "enqueued"
                        }
                      >
                        {reingestingSourceId === source.id ? t("reingestingSource") : t("reingestSource")}
                      </Button>
                      <Button
                        variant="destructive"
                        size="sm"
                        onClick={() =>
                          handleDeleteSource(
                            source.id,
                            source.title || source.original_url || t("sourceUntitled"),
                          )
                        }
                        disabled={deletingSourceId === source.id || reingestingSourceId === source.id}
                      >
                        {deletingSourceId === source.id ? t("deletingSource") : t("deleteSource")}
                      </Button>
                    </div>
                  </CardContent>
                </Card>
              ))}
              {sources.length === 0 && (
                <p className="text-center text-muted-foreground py-8">{t("noSources")}</p>
              )}
            </div>
          </div>
        )}

        {activeTab === "bible" && <ShowBibleEditor projectId={id} />}

        {activeTab === "episodes" && (
          <div className="space-y-3">
            {episodesLoading && <p className="text-muted-foreground">{t("episodesLoading")}</p>}

            {!episodesLoading && episodes.length === 0 && (
              <p className="text-muted-foreground">{t("noEpisodes")}</p>
            )}

            {!episodesLoading && episodes.map((episode) => (
              <Card key={episode.id}>
                <CardContent className="py-4 flex items-center justify-between gap-4">
                  <div>
                    <Link href={`/episodes/${episode.id}`} className="font-medium hover:text-primary">
                      {episode.title}
                    </Link>
                    <p className="text-sm text-muted-foreground">
                      {new Date(episode.created_at).toLocaleString()}
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    <Badge variant="secondary">{formatDuration(episode.duration_seconds)}</Badge>
                    <Badge variant="outline">{episode.language.toUpperCase()}</Badge>
                    {episode.quality_metrics && (
                      <Badge variant="success">
                        {t("verifiedPct", { pct: Math.round(episode.quality_metrics.verified_fact_pct || 0) })}
                      </Badge>
                    )}
                    <Link href={`/episodes/${episode.id}`}>
                      <Button variant="outline" size="sm">{t("openEpisode")}</Button>
                    </Link>
                  </div>
                </CardContent>
              </Card>
            ))}

            <Link href="/episodes">
              <Button variant="ghost">{t("viewAllEpisodes")}</Button>
            </Link>
          </div>
        )}
      </main>
    </div>
  );
}
