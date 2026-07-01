"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import { withBasePath } from "@/lib/base-path";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Input } from "@/components/ui/input";
import { Plus, Trash2, Users } from "lucide-react";
import { toast } from "sonner";

interface SharedSchema {
  schema_name: string;
  display_name?: string | null;
  created_at?: string | null;
  created_by?: string | null;
  writable?: boolean;
}

interface SharedSchemasResponse {
  private_schema: string;
  shared: SharedSchema[];
  can_create: boolean;
}

/**
 * Extract a human-readable error message from the data plane's error payload.
 * The CP forwards the data plane's status + JSON faithfully, so 400/403 bodies
 * carry the authoritative message we surface inline.
 */
function extractError(body: unknown, fallback: string): string {
  if (body && typeof body === "object") {
    const record = body as Record<string, unknown>;
    for (const field of ["detail", "error", "message", "details"] as const) {
      const value = record[field];
      if (typeof value === "string" && value.trim()) return value;
    }
  }
  return fallback;
}

export function SharedSchemasDialog({
  open,
  onOpenChange,
  onChanged,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onChanged?: () => void;
}) {
  const t = useTranslations("sharedSchemas");

  const [loading, setLoading] = React.useState(false);
  const [schemas, setSchemas] = React.useState<SharedSchema[]>([]);
  const [canCreate, setCanCreate] = React.useState(false);
  const [loadError, setLoadError] = React.useState<string | null>(null);

  // Create form
  const [newSchemaName, setNewSchemaName] = React.useState("");
  const [newDisplayName, setNewDisplayName] = React.useState("");
  const [isCreating, setIsCreating] = React.useState(false);
  const [createError, setCreateError] = React.useState<string | null>(null);

  // Deregister in-flight state (per schema)
  const [deregistering, setDeregistering] = React.useState<string | null>(null);

  // "Delete data" confirm dialog target + type-to-confirm + in-flight state
  const [deleteDataTarget, setDeleteDataTarget] = React.useState<string | null>(null);
  const [deleteDataConfirmText, setDeleteDataConfirmText] = React.useState("");
  const [isDroppingData, setIsDroppingData] = React.useState(false);

  const load = React.useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const resp = await fetch(withBasePath("/api/shared/schemas"), { cache: "no-store" });
      const body = (await resp.json().catch(() => null)) as SharedSchemasResponse | null;
      if (!resp.ok) {
        setLoadError(extractError(body, t("loadError")));
        return;
      }
      setSchemas(body?.shared ?? []);
      setCanCreate(!!body?.can_create);
    } catch {
      setLoadError(t("loadError"));
    } finally {
      setLoading(false);
    }
  }, [t]);

  React.useEffect(() => {
    if (open) load();
  }, [open, load]);

  const handleCreate = async () => {
    if (!newSchemaName.trim()) return;
    setIsCreating(true);
    setCreateError(null);
    try {
      const resp = await fetch(withBasePath("/api/shared/schemas"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          schema_name: newSchemaName.trim(),
          ...(newDisplayName.trim() ? { display_name: newDisplayName.trim() } : {}),
        }),
      });
      const body = await resp.json().catch(() => null);
      if (!resp.ok) {
        // Surface the data plane's 400/403 message inline.
        setCreateError(extractError(body, t("createError")));
        return;
      }
      setNewSchemaName("");
      setNewDisplayName("");
      await load();
      onChanged?.();
      toast.success(t("createSuccess"));
    } catch {
      setCreateError(t("createError"));
    } finally {
      setIsCreating(false);
    }
  };

  const handleDeregister = async (schemaName: string) => {
    setDeregistering(schemaName);
    try {
      const resp = await fetch(
        withBasePath(`/api/shared/schemas/${encodeURIComponent(schemaName)}`),
        { method: "DELETE" }
      );
      const body = await resp.json().catch(() => null);
      if (!resp.ok) {
        toast.error(extractError(body, t("deregisterError")));
        return;
      }
      await load();
      onChanged?.();
      toast.success(t("deregisterSuccess"));
    } catch {
      toast.error(t("deregisterError"));
    } finally {
      setDeregistering(null);
    }
  };

  const closeDeleteDataDialog = () => {
    setDeleteDataTarget(null);
    setDeleteDataConfirmText("");
  };

  const handleDeleteData = async () => {
    const schemaName = deleteDataTarget;
    if (!schemaName || deleteDataConfirmText !== schemaName) return;
    setIsDroppingData(true);
    try {
      const resp = await fetch(
        withBasePath(`/api/shared/schemas/${encodeURIComponent(schemaName)}/data`),
        { method: "DELETE" }
      );
      const body = await resp.json().catch(() => null);
      if (!resp.ok) {
        toast.error(extractError(body, t("deleteDataError")));
        return;
      }
      closeDeleteDataDialog();
      await load();
      onChanged?.();
      toast.success(t("deleteDataSuccess"));
    } catch {
      toast.error(t("deleteDataError"));
    } finally {
      setIsDroppingData(false);
    }
  };

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="sm:max-w-[600px]">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Users className="h-5 w-5" />
              {t("title")}
            </DialogTitle>
            <DialogDescription>{t("description")}</DialogDescription>
          </DialogHeader>

          <div className="space-y-4">
            {/* List */}
            {loading ? (
              <div className="flex items-center justify-center gap-2 py-6 text-sm text-muted-foreground">
                <div className="h-4 w-4 animate-spin rounded-full border-2 border-primary border-t-transparent" />
                <span>{t("loading")}</span>
              </div>
            ) : loadError ? (
              <p className="text-sm text-destructive">{loadError}</p>
            ) : schemas.length === 0 ? (
              <p className="text-sm text-muted-foreground py-2">{t("empty")}</p>
            ) : (
              <ul className="divide-y divide-border rounded-md border border-border">
                {schemas.map((schema) => (
                  <li
                    key={schema.schema_name}
                    className="flex items-center gap-2 px-3 py-2 text-sm"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="font-medium truncate" title={schema.schema_name}>
                        {schema.display_name || schema.schema_name}
                      </div>
                      <div className="text-xs text-muted-foreground font-mono truncate">
                        {schema.schema_name}
                        {schema.writable === false ? ` · ${t("readOnly")}` : ""}
                      </div>
                    </div>
                    {canCreate && (
                      <div className="flex items-center gap-1 shrink-0">
                        <Button
                          variant="outline"
                          size="sm"
                          disabled={deregistering === schema.schema_name}
                          onClick={() => handleDeregister(schema.schema_name)}
                          title={t("deregisterHint")}
                        >
                          {deregistering === schema.schema_name
                            ? t("deregistering")
                            : t("deregister")}
                        </Button>
                        <Button
                          variant="destructive"
                          size="sm"
                          className="gap-1"
                          onClick={() => setDeleteDataTarget(schema.schema_name)}
                          title={t("deleteDataHint")}
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                          {t("deleteData")}
                        </Button>
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            )}

            {/* Create form (admin only, gated by can_create) */}
            {canCreate && (
              <div className="rounded-md border border-border p-3 space-y-3">
                <div className="flex items-center gap-2 text-sm font-semibold">
                  <Plus className="h-4 w-4" />
                  {t("createTitle")}
                </div>
                <Input
                  placeholder={t("schemaNamePlaceholder")}
                  value={newSchemaName}
                  onChange={(e) => {
                    setNewSchemaName(e.target.value);
                    setCreateError(null);
                  }}
                />
                <Input
                  placeholder={t("displayNamePlaceholder")}
                  value={newDisplayName}
                  onChange={(e) => setNewDisplayName(e.target.value)}
                />
                <p className="text-xs text-muted-foreground">{t("schemaNameHint")}</p>
                {createError && <p className="text-sm text-destructive">{createError}</p>}
                <div className="flex justify-end">
                  <Button onClick={handleCreate} disabled={isCreating || !newSchemaName.trim()}>
                    {isCreating ? t("creating") : t("create")}
                  </Button>
                </div>
              </div>
            )}
          </div>
        </DialogContent>
      </Dialog>

      {/* Two-step delete: step 2 "Delete data" — destructive, irreversible.
          Proxies to DELETE /api/shared/schemas/{schema}/data which the data
          plane maps to DROP SCHEMA ... CASCADE. Guarded behind a type-to-confirm
          (the operator must type the exact schema name). */}
      <AlertDialog
        open={deleteDataTarget !== null}
        onOpenChange={(o) => {
          if (!o && !isDroppingData) closeDeleteDataDialog();
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("deleteDataTitle")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("deleteDataDescription", { schema: deleteDataTarget ?? "" })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <div className="space-y-2">
            <p className="text-sm text-destructive font-medium">{t("deleteDataWarning")}</p>
            <p className="text-sm text-muted-foreground">
              {t("deleteDataConfirmPrompt", { schema: deleteDataTarget ?? "" })}
            </p>
            <Input
              value={deleteDataConfirmText}
              onChange={(e) => setDeleteDataConfirmText(e.target.value)}
              placeholder={deleteDataTarget ?? ""}
              autoComplete="off"
              disabled={isDroppingData}
            />
          </div>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={isDroppingData}>{t("cancel")}</AlertDialogCancel>
            <AlertDialogAction
              disabled={isDroppingData || deleteDataConfirmText !== deleteDataTarget}
              onClick={(e) => {
                // Prevent the AlertDialog from auto-closing; we close on success.
                e.preventDefault();
                void handleDeleteData();
              }}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {isDroppingData ? t("deleteDataInProgress") : t("deleteDataConfirm")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
