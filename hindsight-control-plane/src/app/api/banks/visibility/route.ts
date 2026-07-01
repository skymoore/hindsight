import { NextRequest, NextResponse } from "next/server";
import { localizeApiErrorPayload } from "@/lib/i18n/api-errors";
import { DATAPLANE_URL, getDataplaneHeaders } from "@/lib/hindsight-client";

export async function GET(request: NextRequest) {
  try {
    // Direct fetch since the SDK doesn't have this operation yet.
    const url = `${DATAPLANE_URL}/v1/default/banks/visibility`;
    const response = await fetch(url, {
      headers: getDataplaneHeaders(request),
    });

    const data = await response.json();
    if (!response.ok) {
      return NextResponse.json(data, { status: response.status });
    }

    return NextResponse.json(data, { status: 200 });
  } catch (error) {
    console.error("Error fetching banks visibility:", error);
    return NextResponse.json(
      localizeApiErrorPayload(request, {
        error: "Failed to fetch banks visibility",
        errorKey: "api.errors.banks.visibilityList",
      }),
      { status: 500 }
    );
  }
}
