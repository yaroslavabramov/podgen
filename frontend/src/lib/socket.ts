type EventHandler = (data: Record<string, unknown>) => void;

export function createGenerationSocket(runId: string, onEvent: EventHandler) {
  const token = typeof window !== "undefined" ? localStorage.getItem("token") || "" : "";
  const wsUrl = `${process.env.NEXT_PUBLIC_WS_URL || "ws://localhost:8000"}/api/ws/generations/${runId}?token=${encodeURIComponent(token)}`;
  const ws = new WebSocket(wsUrl);

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      onEvent(data);
    } catch {
      // ignore parse errors
    }
  };

  ws.onerror = (error) => {
    console.error("WebSocket error:", error);
  };

  return {
    close: () => ws.close(),
    ws,
  };
}
