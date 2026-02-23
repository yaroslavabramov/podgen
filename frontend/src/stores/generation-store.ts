"use client";

import { create } from "zustand";
import { createGenerationSocket } from "@/lib/socket";

interface GenerationEvent {
  type: string;
  phase?: string;
  step?: string;
  message?: string;
  pct?: number;
  [key: string]: unknown;
}

interface GenerationStore {
  runId: string | null;
  status: string;
  currentPhase: string;
  currentStep: string;
  progressPct: number;
  events: GenerationEvent[];
  episodeId: string | null;
  error: string | null;
  socket: { close: () => void } | null;
  startListening: (runId: string) => void;
  stopListening: () => void;
  reset: () => void;
}

export const useGenerationStore = create<GenerationStore>((set, get) => ({
  runId: null,
  status: "idle",
  currentPhase: "",
  currentStep: "",
  progressPct: 0,
  events: [],
  episodeId: null,
  error: null,
  socket: null,

  startListening: (runId) => {
    const existing = get().socket;
    if (existing) existing.close();

    const socket = createGenerationSocket(runId, (data) => {
      const event = data as GenerationEvent;
      set((state) => {
        const updates: Partial<GenerationStore> = {
          events: [...state.events, event],
        };

        switch (event.type) {
          case "phase_started":
            updates.currentPhase = event.phase || "";
            updates.status = "running";
            break;
          case "step_started":
            updates.currentStep = event.step || "";
            break;
          case "progress":
            updates.progressPct = event.pct || 0;
            break;
          case "generation_completed":
            updates.status = "completed";
            updates.episodeId = (event.episode_id as string) || null;
            updates.progressPct = 100;
            break;
          case "generation_failed":
            updates.status = "failed";
            updates.error = (event.error as string) || "Unknown error";
            break;
        }

        return updates;
      });
    });

    set({ runId, socket, status: "connecting", events: [], error: null, episodeId: null });
  },

  stopListening: () => {
    const socket = get().socket;
    if (socket) socket.close();
    set({ socket: null });
  },

  reset: () => {
    const socket = get().socket;
    if (socket) socket.close();
    set({
      runId: null, status: "idle", currentPhase: "", currentStep: "",
      progressPct: 0, events: [], episodeId: null, error: null, socket: null,
    });
  },
}));
