"use client";

import { useState, useEffect } from "react";
import { useTranslations } from "use-intl";
import { HTTPError } from "ky";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";

interface ShowBible {
  id: string;
  audience_profile: Record<string, unknown>;
  tone_and_mood: string;
  show_style: string;
  hosts: Array<Record<string, unknown>>;
  forbidden_phrases: string[];
  rigor_policy: string | null;
  adjacency_policy: string | null;
  explanation_policy: string | null;
  format_rules: Record<string, unknown>;
}

const SHOW_STYLES = ["Investigative", "Conversational", "Academic Debate", "Explainer"];
const AUDIENCE_LEVELS = ["beginner", "casual", "professional"];
const TONES = ["serious", "light", "comedic", "investigative"];

export function ShowBibleEditor({ projectId }: { projectId: string }) {
  const t = useTranslations("showBibleEditor");
  const [bible, setBible] = useState<ShowBible | null>(null);
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState(false);
  const [generateError, setGenerateError] = useState<string | null>(null);
  const [showStyle, setShowStyle] = useState("Conversational");
  const [audienceLevel, setAudienceLevel] = useState("casual");
  const [tone, setTone] = useState("light");
  const [topic, setTopic] = useState("");

  useEffect(() => {
    api.get(`api/projects/${projectId}/show-bible`).json<ShowBible>()
      .then((data) => {
        setBible(data);
        setShowStyle(data.show_style);
        setTone(data.tone_and_mood);
        setAudienceLevel((data.audience_profile as { level?: string })?.level || "casual");
      })
      .catch(() => setBible(null))
      .finally(() => setLoading(false));
  }, [projectId]);

  const extractApiError = async (error: unknown, fallback: string) => {
    if (error instanceof HTTPError) {
      try {
        const payload = await error.response.clone().json() as { detail?: string };
        if (payload?.detail) return payload.detail;
      } catch {
        // ignore parse failures and fallback to status text/message
      }
      return error.message || fallback;
    }

    if (error instanceof Error) {
      if (error.name === "TimeoutError") return t("generateTimeout");
      return error.message || fallback;
    }

    return fallback;
  };

  const handleGenerate = async () => {
    setGenerating(true);
    setGenerateError(null);
    try {
      const data = await api.post(`api/projects/${projectId}/show-bible/generate`, {
        json: { topic, audience_level: audienceLevel, show_style: showStyle },
        timeout: false,
      }).json<ShowBible>();
      setBible(data);
    } catch (error) {
      setGenerateError(await extractApiError(error, t("generateFailed")));
    } finally {
      setGenerating(false);
    }
  };

  const handleSave = async () => {
    await api.post(`api/projects/${projectId}/show-bible`, {
      json: {
        show_style: showStyle,
        tone_and_mood: tone,
        audience_profile: { level: audienceLevel, knows: [], explain: [] },
      },
    }).json<ShowBible>().then(setBible);
  };

  if (loading) return <p className="text-muted-foreground">{t("loading")}</p>;

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle>{t("aiGeneration")}</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <Input
            placeholder={t("topicPlaceholder")}
            value={topic}
            onChange={(e) => setTopic(e.target.value)}
          />
          <div className="flex gap-3">
            <select value={audienceLevel} onChange={(e) => setAudienceLevel(e.target.value)}
              className="rounded-md border border-border px-3 py-2 text-sm bg-background">
              {AUDIENCE_LEVELS.map((l) => <option key={l} value={l}>{l}</option>)}
            </select>
            <select value={showStyle} onChange={(e) => setShowStyle(e.target.value)}
              className="rounded-md border border-border px-3 py-2 text-sm bg-background">
              {SHOW_STYLES.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
            <Button type="button" onClick={handleGenerate} disabled={!topic.trim() || generating}>
              {generating ? t("generating") : t("generateWithAi")}
            </Button>
          </div>
          {generateError && (
            <p className="text-sm text-destructive">{generateError}</p>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t("showConfiguration")}</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid grid-cols-3 gap-4">
            <div>
              <label className="text-sm font-medium mb-1 block">{t("showStyle")}</label>
              <select value={showStyle} onChange={(e) => setShowStyle(e.target.value)}
                className="w-full rounded-md border border-border px-3 py-2 text-sm bg-background">
                {SHOW_STYLES.map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
            </div>
            <div>
              <label className="text-sm font-medium mb-1 block">{t("audienceLevel")}</label>
              <select value={audienceLevel} onChange={(e) => setAudienceLevel(e.target.value)}
                className="w-full rounded-md border border-border px-3 py-2 text-sm bg-background">
                {AUDIENCE_LEVELS.map((l) => <option key={l} value={l}>{l}</option>)}
              </select>
            </div>
            <div>
              <label className="text-sm font-medium mb-1 block">{t("tone")}</label>
              <select value={tone} onChange={(e) => setTone(e.target.value)}
                className="w-full rounded-md border border-border px-3 py-2 text-sm bg-background">
                {TONES.map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
            </div>
          </div>

          {bible?.hosts && bible.hosts.length > 0 && (
            <div>
              <label className="text-sm font-medium mb-2 block">{t("hostPersonas")}</label>
              <div className="grid grid-cols-2 gap-4">
                {bible.hosts.map((host, i) => (
                  <div key={i} className="p-3 rounded-md border border-border text-sm">
                    <p className="font-medium">{(host as { name?: string }).name || t("hostFallback", { index: i + 1 })}</p>
                    <p className="text-muted-foreground">
                      {t("role", { role: (host as { role?: string }).role || t("notAvailable") })}
                    </p>
                    <p className="text-muted-foreground mt-1">{(host as { interaction_style?: string }).interaction_style || ""}</p>
                  </div>
                ))}
              </div>
            </div>
          )}

          {bible?.forbidden_phrases && bible.forbidden_phrases.length > 0 && (
            <div>
              <label className="text-sm font-medium mb-1 block">{t("forbiddenPhrases")}</label>
              <div className="flex flex-wrap gap-2">
                {bible.forbidden_phrases.map((phrase, i) => (
                  <span key={i} className="px-2 py-1 bg-destructive/10 text-destructive text-xs rounded">{phrase}</span>
                ))}
              </div>
            </div>
          )}

          <Button type="button" onClick={handleSave}>{t("saveConfiguration")}</Button>
        </CardContent>
      </Card>
    </div>
  );
}
