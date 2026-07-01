import { NextResponse } from "next/server";
import { localizeApiErrorPayload } from "@/lib/i18n/api-errors";
import { DATAPLANE_URL, getDataplaneHeaders } from "@/lib/hindsight-client";

/**
 * Shared memory-bank schema management (BFF).
 *
 * These routes are intentionally auth-agnostic: they forward the caller's
 * identity to the data plane, which is the sole authority on admin-only access.
 * We do NOT enforce the `shared_` prefix or admin role here — the data plane
 * returns 400/403 and we pass those through faithfully.
 *
 * The generated SDK does not yet expose these endpoints, so we call the data
 * plane directly with `fetch`, mirroring the auth-forwarding pattern.
 */

export async function GET(request: Request) {
  try {
    const upstream = await fetch(`${DATAPLANE_URL}/v1/shared/schemas`, {
      headers: getDataplaneHeaders(request, { "Content-Type": "application/json" }),
      cache: "no-store",
    });
    const data = await upstream.json().catch(() => null);
    return NextResponse.json(data, { status: upstream.status });
  } catch (error) {
    console.error("Error fetching shared schemas:", error);
    return NextResponse.json(
      localizeApiErrorPayload(request, {
        error: "Failed to fetch shared schemas",
        errorKey: "api.errors.sharedSchemas.fetch",
      }),
      { status: 502 }
    );
  }
}

export async function POST(request: Request) {
  let body;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json(
      localizeApiErrorPayload(request, {
        error: "Invalid JSON body",
        errorKey: "api.errors.auth.invalidRequestBody",
      }),
      { status: 400 }
    );
  }

  const { schema_name, display_name } = body ?? {};

  // Client-side presence check only. The `shared_` prefix and admin
  // enforcement are the data plane's responsibility (it returns 400/403).
  if (!schema_name || typeof schema_name !== "string" || !schema_name.trim()) {
    return NextResponse.json(
      localizeApiErrorPayload(request, {
        error: "schema_name is required",
        errorKey: "api.errors.sharedSchemas.validation.schemaNameRequired",
      }),
      { status: 400 }
    );
  }

  try {
    const upstream = await fetch(`${DATAPLANE_URL}/v1/shared/schemas`, {
      method: "POST",
      headers: getDataplaneHeaders(request, { "Content-Type": "application/json" }),
      body: JSON.stringify({
        schema_name: schema_name.trim(),
        ...(display_name ? { display_name } : {}),
      }),
      cache: "no-store",
    });
    const data = await upstream.json().catch(() => null);
    return NextResponse.json(data, { status: upstream.status });
  } catch (error) {
    console.error("Error creating shared schema:", error);
    return NextResponse.json(
      localizeApiErrorPayload(request, {
        error: "Failed to create shared schema",
        errorKey: "api.errors.sharedSchemas.create",
      }),
      { status: 502 }
    );
  }
}
