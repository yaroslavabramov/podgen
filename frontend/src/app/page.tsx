import Link from "next/link";
import { Button } from "@/components/ui/button";

export default function LandingPage() {
  return (
    <div className="min-h-screen flex flex-col">
      <header className="border-b border-border px-6 py-4 flex items-center justify-between">
        <h1 className="text-xl font-bold text-primary">Podgen</h1>
        <div className="flex gap-3">
          <Link href="/login">
            <Button variant="ghost">Log in</Button>
          </Link>
          <Link href="/login">
            <Button>Get Started</Button>
          </Link>
        </div>
      </header>

      <main className="flex-1 flex flex-col items-center justify-center px-6 text-center">
        <div className="max-w-3xl space-y-6">
          <h2 className="text-5xl font-bold tracking-tight">
            AI-Powered Podcast Generation
          </h2>
          <p className="text-xl text-muted-foreground">
            Upload your research, configure your show style, and let AI generate
            a professional 2-host podcast with fact-checked content, natural dialogue,
            and studio-quality audio.
          </p>

          <div className="flex gap-4 justify-center pt-4">
            <Link href="/login">
              <Button size="lg">Start Creating</Button>
            </Link>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-6 pt-12 text-left">
            <div className="p-6 rounded-lg border border-border">
              <h3 className="font-semibold mb-2">Evidence-First Research</h3>
              <p className="text-sm text-muted-foreground">
                Every claim is traced to sources. Facts are verified, opinions attributed,
                contradictions addressed honestly.
              </p>
            </div>
            <div className="p-6 rounded-lg border border-border">
              <h3 className="font-semibold mb-2">Natural 2-Host Dialogue</h3>
              <p className="text-sm text-muted-foreground">
                Guide and Challenger dynamics create engaging conversation with real personality
                and audience-appropriate explanations.
              </p>
            </div>
            <div className="p-6 rounded-lg border border-border">
              <h3 className="font-semibold mb-2">15-90+ Minutes</h3>
              <p className="text-sm text-muted-foreground">
                Configurable duration with smart content layering. From quick explainers
                to deep investigative episodes.
              </p>
            </div>
          </div>
        </div>
      </main>
    </div>
  );
}
