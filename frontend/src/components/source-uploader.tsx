"use client";

import { useState } from "react";
import { useTranslations } from "use-intl";
import * as tus from "tus-js-client";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent } from "@/components/ui/card";

interface Props {
  projectId: string;
  onUploaded: () => void;
}

export function SourceUploader({ projectId, onUploaded }: Props) {
  const t = useTranslations("sourceUploader");
  const [url, setUrl] = useState("");
  const [uploading, setUploading] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [uploadProgress, setUploadProgress] = useState<number | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);

  const uploadFileClassic = async (file: File) => {
    const formData = new FormData();
    formData.append("file", file);
    const response = await fetch(
      `${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/api/projects/${projectId}/sources/upload`,
      {
        method: "POST",
        headers: { Authorization: `Bearer ${localStorage.getItem("token")}` },
        body: formData,
      }
    );
    if (!response.ok) {
      let detail = "";
      try {
        detail = await response.text();
      } catch {
        // Ignore body read errors.
      }
      throw new Error(detail || `Upload failed (${response.status})`);
    }
  };

  const uploadFileTus = (file: File) => {
    return new Promise<void>((resolve, reject) => {
      const token = localStorage.getItem("token");
      if (!token) {
        reject(new Error("Missing auth token"));
        return;
      }

      const upload = new tus.Upload(file, {
        endpoint: `${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/api/projects/${projectId}/sources/upload/resumable`,
        metadata: {
          filename: file.name,
          filetype: file.type || "application/octet-stream",
        },
        headers: {
          Authorization: `Bearer ${token}`,
        },
        retryDelays: [0, 1000, 3000, 5000, 10000],
        chunkSize: 5 * 1024 * 1024,
        removeFingerprintOnSuccess: true,
        onProgress: (uploaded, total) => {
          const pct = total > 0 ? Math.round((uploaded / total) * 100) : 0;
          setUploadProgress(pct);
        },
        onSuccess: () => resolve(),
        onError: (error) => reject(error),
      });

      upload.findPreviousUploads()
        .then((previousUploads) => {
          if (previousUploads.length > 0) {
            upload.resumeFromPreviousUpload(previousUploads[0]);
          }
          upload.start();
        })
        .catch(reject);
    });
  };

  const handleFileUpload = async (files: FileList) => {
    setUploading(true);
    setUploadProgress(0);
    setUploadError(null);
    try {
      for (const file of Array.from(files)) {
        try {
          await uploadFileTus(file);
        } catch {
          // Fallback to classic upload for compatibility.
          setUploadProgress(null);
          await uploadFileClassic(file);
        }
      }
      onUploaded();
    } catch (err) {
      const message = err instanceof Error ? err.message : t("uploadFailed");
      setUploadError(message);
    } finally {
      setUploading(false);
      setUploadProgress(null);
    }
  };

  const handleUrlAdd = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!url.trim()) return;
    setUploading(true);
    try {
      await api.post(`api/projects/${projectId}/sources/url`, { json: { url, source_type: "web" } });
      setUrl("");
      onUploaded();
    } finally {
      setUploading(false);
    }
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    if (e.dataTransfer.files.length) handleFileUpload(e.dataTransfer.files);
  };

  return (
    <div className="space-y-4">
      <Card
        className={`border-2 border-dashed transition-colors ${dragOver ? "border-primary bg-primary/5" : "border-border"}`}
        onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={handleDrop}
      >
        <CardContent className="py-8 text-center">
          <p className="text-muted-foreground mb-3">
            {uploading
              ? uploadProgress === null
                ? t("uploading")
                : t("uploadingProgress", { pct: uploadProgress })
              : t("dropOrBrowse")}
          </p>
          <input
            type="file"
            multiple
            accept=".pdf,.mp3,.wav,.m4a,.mp4,.webm"
            onChange={(e) => e.target.files && handleFileUpload(e.target.files)}
            className="hidden"
            id="file-upload"
          />
          <label htmlFor="file-upload">
            <Button variant="outline" asChild disabled={uploading}>
              <span>{t("browseFiles")}</span>
            </Button>
          </label>
          <p className="text-xs text-muted-foreground mt-2">{t("formats")}</p>
        </CardContent>
      </Card>
      {uploadError && (
        <p className="text-sm text-destructive">{uploadError}</p>
      )}

      <form onSubmit={handleUrlAdd} className="flex gap-3">
        <Input
          placeholder={t("addUrlPlaceholder")}
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          className="flex-1"
        />
        <Button type="submit" variant="outline" disabled={!url.trim() || uploading}>
          {t("addUrl")}
        </Button>
      </form>
    </div>
  );
}
