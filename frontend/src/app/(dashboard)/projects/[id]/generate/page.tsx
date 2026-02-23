"use client";

import { useState, useEffect } from "react";
import { useParams, useRouter } from "next/navigation";
import { api, type CostEstimate } from "@/lib/api";
import { useGenerationStore } from "@/stores/generation-store";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { GenerationProgress } from "@/components/generation-progress";

export default function GeneratePage() {
  const params = useParams();
  const router = useRouter();
  const projectId = params.id as string;

  const [prompt, setPrompt] = useState("");
  const [duration, setDuration] = useState(1800);
  const [cost, setCost] = useState<CostEstimate | null>(null);
  const [launching, setLaunching] = useState(false);

  const { runId, status, startListening } = useGenerationStore();

  useEffect(() => {
    if (!prompt) return;
    const timer = setTimeout(async () => {
      try {
        const estimate = await api.post(`api/projects/${projectId}/estimate`, {
          json: { duration_target: duration },
        }).json<CostEstimate>();
        setCost(estimate);
      } catch {
        // ignore
      }
    }, 500);
    return () => clearTimeout(timer);
  }, [duration, prompt, projectId]);

  const handleGenerate = async () => {
    setLaunching(true);
    try {
      const run = await api.post(`api/projects/${projectId}/generate`, {
        json: { duration_target: duration, user_prompt: prompt },
      }).json<{ id: string }>();
      startListening(run.id);
    } catch (err) {
      console.error("Failed to start generation:", err);
    } finally {
      setLaunching(false);
    }
  };

  if (runId && status !== "idle") {
    return (
      <div className="min-h-screen bg-background">
        <header className="border-b border-border px-6 py-4">
          <h1 className="text-xl font-bold">Generating Podcast</h1>
        </header>
        <main className="max-w-4xl mx-auto px-6 py-8">
          <GenerationProgress />
        </main>
      </div>
    );
  }

  const durationMinutes = duration / 60;
  const durationLabels: Record<number, string> = {
    900: "15 min", 1800: "30 min", 2700: "45 min", 3600: "60 min", 5400: "90 min",
  };

  return (
    <div className="min-h-screen bg-background">
      <header className="border-b border-border px-6 py-4">
        <h1 className="text-xl font-bold">Generate Podcast</h1>
      </header>

      <main className="max-w-3xl mx-auto px-6 py-8 space-y-6">
        <Card>
          <CardHeader>
            <CardTitle>Topic & Prompt</CardTitle>
          </CardHeader>
          <CardContent>
            <Textarea
              placeholder="Describe the podcast topic in detail. What should the hosts discuss? What angle should they take?"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              rows={4}
            />
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Duration</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="flex gap-3">
              {[900, 1800, 2700, 3600, 5400].map((d) => (
                <button
                  key={d}
                  onClick={() => setDuration(d)}
                  className={`px-4 py-2 rounded-md text-sm font-medium border transition-colors ${
                    duration === d
                      ? "border-primary bg-primary/10 text-primary"
                      : "border-border hover:border-primary/50"
                  }`}
                >
                  {durationLabels[d]}
                </button>
              ))}
            </div>
          </CardContent>
        </Card>

        {cost && (
          <Card>
            <CardHeader>
              <CardTitle>Cost Estimate</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="grid grid-cols-3 gap-4 text-center">
                <div>
                  <p className="text-2xl font-bold">${cost.estimated_llm_cost.toFixed(2)}</p>
                  <p className="text-sm text-muted-foreground">LLM Cost</p>
                </div>
                <div>
                  <p className="text-2xl font-bold">${cost.estimated_tts_cost.toFixed(2)}</p>
                  <p className="text-sm text-muted-foreground">TTS Cost</p>
                </div>
                <div>
                  <p className="text-2xl font-bold text-primary">${cost.estimated_total.toFixed(2)}</p>
                  <p className="text-sm text-muted-foreground">Total</p>
                </div>
              </div>
            </CardContent>
          </Card>
        )}

        <Button
          size="lg"
          className="w-full"
          onClick={handleGenerate}
          disabled={!prompt.trim() || launching}
        >
          {launching ? "Starting..." : "Start Generation"}
        </Button>
      </main>
    </div>
  );
}
