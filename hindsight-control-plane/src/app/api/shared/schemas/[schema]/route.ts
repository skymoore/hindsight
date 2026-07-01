import { NextRequest, NextResponse } from "next/server";
import { localizeApiErrorPayload } from "@/lib/i18n/api-errors";
import { DATAPLANE_URL, getDataplaneHeaders } from "@/lib/hindsight-client";

/**
 * Deregister a shared memory-bank schema (BFF).
 *
 * Auth-agnostic: forwards the caller's identity to the data plane, which
 * enforces admin-only and returns 403 (not admin) / 404 (not found). We pass
 * upstream status + JSON through faithfully.
 */
export async function DELETE(
  request: NextRequest,
  { params }: { params: Promise<{ schema: string }> }
) {
  const { schema } = await params;

  try {
    const upstream = await fetch(
      `${DATAPLANE_URL}/v1/shared/schemas/${encodeURIComponent(schema)}`,
      {
        method: "DELETE",
        headers: getDataplaneHeaders(request, { "Content-Type": "application/json" }),
        cache: "no-store",
      }
    );
    const data = await upstream.json().catch(() => null);
    return NextResponse.json(data, { status: upstream.status });
  } catch (error) {
    console.error("Error deleting shared schema:", error);
    return NextResponse.json(
      localizeApiErrorPayload(request, {
        error: "Failed to delete shared schema",
        errorKey: "api.errors.sharedSchemas.delete",
      }),
      { status: 502 }
    );
  }
}
