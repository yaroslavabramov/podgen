import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Podgen — AI Podcast Generation",
  description: "Generate long-form, research-backed podcasts with AI",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="bg-background text-foreground antialiased">{children}</body>
    </html>
  );
}
