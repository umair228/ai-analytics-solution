"""
AI insights for forecasts — grounded in the forecast payload + live DB facts.

    POST /api/forecast-insights/
    {
      "feature": "downtime" | "sample" | "section" | "inventory",
      "params":  { ... the query params used for the forecast ... },
      "forecast": { ... the forecast response payload (optional) ... },
      "question": "free-form question"   # optional; omit for an auto brief
    }
    -> { "configured": true, "answer": "...", "suggestions": [...] }
"""
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from ai.client import AINotConfigured, is_configured, run_chat
from ..insights import build_context, suggested_questions, FEATURE_LABEL

BRIEF_PROMPT = (
    "You are briefing a laboratory operations manager on the forecast above. "
    "Write a tight, practical insight brief with these short labelled sections:\n"
    "1. Headline — the single most important takeaway in one sentence.\n"
    "2. What the forecast shows — the trajectory, peaks/troughs and magnitude.\n"
    "3. Risks & watch-outs — anything concerning (limit breaches, spikes, stock-outs).\n"
    "4. Recommended actions — two or three concrete operational steps.\n\n"
    "Ground every point in the actual numbers and the live database facts provided. "
    "Do not invent figures. Keep it under ~220 words; light bullets are fine."
)


class ForecastInsightsView(APIView):
    def post(self, request):
        if not is_configured():
            return Response(
                {"configured": False,
                 "answer": "AI insights are unavailable — set CLAUDE_API_KEY in the backend .env.",
                 "suggestions": []},
                status=status.HTTP_200_OK,
            )

        data = request.data or {}
        feature = data.get("feature", "")
        params = data.get("params") or {}
        forecast = data.get("forecast") or {}
        question = (data.get("question") or "").strip()

        try:
            context = build_context(feature, params, forecast)
            label = FEATURE_LABEL.get((feature or "").lower(), "forecast")
            if question:
                user_msg = (
                    f"This is a {label} forecast. Answer the user's question grounded strictly in "
                    f"the forecast data and live database facts above.\n\nQuestion: {question}"
                )
            else:
                user_msg = BRIEF_PROMPT
            answer = run_chat([{"role": "user", "content": user_msg}], dataset_context=context)
            return Response(
                {"configured": True, "answer": answer, "suggestions": suggested_questions(feature)},
                status=status.HTTP_200_OK,
            )
        except AINotConfigured:
            return Response(
                {"configured": False, "answer": "AI insights are not configured.", "suggestions": []},
                status=status.HTTP_200_OK,
            )
        except Exception as e:
            print(f"[forecast-insights] error: {e}")
            return Response(
                {"configured": True, "answer": f"Could not generate insights: {e}", "suggestions": []},
                status=status.HTTP_200_OK,
            )
