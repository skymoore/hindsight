import { NextRequest, NextResponse } from "next/server";
import { localizeApiErrorPayload } from "@/lib/i18n/api-errors";
import { DATAPLANE_URL, getDataplaneHeaders } from "@/lib/hindsight-client";

/**
 * Permanently delete a shared memory-bank schema AND all its data (BFF).
 *
 * DESTRUCTIVE / IRREVERSIBLE: proxies to the data plane's
 * `DELETE /v1/shared/schemas/{schema}/data`, which deregisters the schema and
 * runs `DROP SCHEMA ... CASCADE`.
 *
 * Auth-agnostic: forwards the caller's identity; the data plane enforces
 * admin-only and returns 403 (not admin) / 404 (not found). We pass the
 * upstream status + JSON through faithfully.
 */
export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ schema: string }> }
) {
  const { schema } = await params;

  try {
    const upstream = await fetch(
      `${DATAPLANE_URL}/v1/shared/schemas/${encodeURIComponent(schema)}/data`,
      {
        method: "DELETE",
        headers: getDataplaneHeaders(request, { "Content-Type": "application/json" }),
        cache: "no-store",
      }
    );
    const data = await upstream.json().catch(() => null);
    return NextResponse.json(data, { status: upstream.status });
  } catch (error) {
    console.error("Error dropping shared schema data:", error);
    return NextResponse.json(
      localizeApiErrorPayload(request, {
        error: "Failed to delete shared schema data",
        errorKey: "api.errors.sharedSchemas.dropData",
      }),
      { status: 502 }
    );
  }
}
