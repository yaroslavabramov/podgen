import ky from "ky";

export const api = ky.create({
  prefixUrl: process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000",
  hooks: {
    beforeRequest: [
      (request) => {
        const token = typeof window !== "undefined" ? localStorage.getItem("token") : null;
        if (token) {
          request.headers.set("Authorization", `Bearer ${token}`);
        }
      },
    ],
  },
});

export interface Project {
  id: string;
  user_id: string;
  title: string;
  description: string | null;
  language: string;
  created_at: string;
  updated_at: string;
  source_count: number;
  episode_count: number;
}

export interface Source {
  id: string;
  project_id: string;
  source_type: string;
  original_url: string | null;
  title: string | null;
  ingestion_status: string;
  is_temporary: boolean;
  created_at: string;
}

export interface Episode {
  id: string;
  project_id: string;
  title: string;
  description: string | null;
  language: string;
  duration_seconds: number;
  audio_file_path: string | null;
  transcript_json: Array<{ start_ms: number; end_ms: number; speaker: string; text: string }> | null;
  shownotes_json: Record<string, unknown> | null;
  quality_metrics: Record<string, number> | null;
  created_at: string;
}

export interface GenerationRun {
  id: string;
  project_id: string;
  status: string;
  current_phase: string | null;
  current_step: string | null;
  progress_pct: number | null;
  estimated_cost: { estimated_llm_cost: number; estimated_tts_cost: number; estimated_total: number } | null;
  created_at: string;
}

export interface CostEstimate {
  estimated_llm_cost: number;
  estimated_tts_cost: number;
  estimated_total: number;
  estimated_minutes: number;
}
