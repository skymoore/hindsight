import { NextRequest, NextResponse } from "next/server";
import { localizeApiErrorPayload } from "@/lib/i18n/api-errors";
import { dataplaneBankUrl, getDataplaneHeaders } from "@/lib/hindsight-client";

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ bankId: string }> }
) {
  try {
    const { bankId } = await params;

    // Direct fetch since the SDK doesn't have this operation yet.
    const url = dataplaneBankUrl(bankId, "/visibility");
    const response = await fetch(url, {
      headers: getDataplaneHeaders(request),
    });

    const data = await response.json();
    if (!response.ok) {
      return NextResponse.json(data, { status: response.status });
    }

    return NextResponse.json(data, { status: 200 });
  } catch (error) {
    console.error("Error fetching bank visibility:", error);
    return NextResponse.json(
      localizeApiErrorPayload(request, {
        error: "Failed to fetch bank visibility",
        errorKey: "api.errors.banks.visibilityFetch",
      }),
      { status: 500 }
    );
  }
}

export async function PUT(
  request: NextRequest,
  { params }: { params: Promise<{ bankId: string }> }
) {
  try {
    const { bankId } = await params;
    const body = await request.json();

    if (body?.visibility !== "private" && body?.visibility !== "shared") {
      return NextResponse.json(
        localizeApiErrorPayload(request, {
          error: "visibility must be 'private' or 'shared'",
          errorKey: "api.errors.banks.visibilityUpdate",
        }),
        { status: 400 }
      );
    }

    // Direct fetch since the SDK doesn't have this operation yet.
    const url = dataplaneBankUrl(bankId, "/visibility");
    const response = await fetch(url, {
      method: "PUT",
      headers: getDataplaneHeaders(request, { "Content-Type": "application/json" }),
      body: JSON.stringify({ visibility: body?.visibility }),
    });

    const data = await response.json();
    if (!response.ok) {
      return NextResponse.json(data, { status: response.status });
    }

    return NextResponse.json(data, { status: 200 });
  } catch (error) {
    console.error("Error updating bank visibility:", error);
    return NextResponse.json(
      localizeApiErrorPayload(request, {
        error: "Failed to update bank visibility",
        errorKey: "api.errors.banks.visibilityUpdate",
      }),
      { status: 500 }
    );
  }
}
