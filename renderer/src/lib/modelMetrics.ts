import type { TraceEvent } from '@/types'

export interface ModelMetrics {
  latencyMs: number | null
  ttftMs: number | null
  promptTokens: number | null
  completionTokens: number | null
  totalTokens: number | null
  cachedTokens: number | null
  cacheHitRatio: number | null
  promptCacheKey: string
}

export function modelMetricsFromEvent(event: TraceEvent | null | undefined): ModelMetrics | null {
  if (!event || event.kind !== 'model') return null
  const payload = event.payload ?? {}
  const metrics: ModelMetrics = {
    latencyMs: numberFrom(payload.latency_ms),
    ttftMs: numberFrom(payload.ttft_ms),
    promptTokens: numberFrom(payload.prompt_tokens),
    completionTokens: numberFrom(payload.completion_tokens),
    totalTokens: numberFrom(payload.total_tokens ?? event.token_count),
    cachedTokens: numberFrom(payload.cached_tokens),
    cacheHitRatio: numberFrom(payload.cache_hit_ratio),
    promptCacheKey: stringFrom(payload.prompt_cache_key)
  }
  if (
    metrics.latencyMs == null &&
    metrics.ttftMs == null &&
    metrics.promptTokens == null &&
    metrics.completionTokens == null &&
    metrics.totalTokens == null &&
    metrics.cachedTokens == null &&
    metrics.cacheHitRatio == null &&
    !metrics.promptCacheKey
  ) {
    return null
  }
  return metrics
}

export function modelMetricItems(metrics: ModelMetrics) {
  return [
    { label: 'latency', value: formatMs(metrics.latencyMs) },
    { label: 'TTFT', value: formatMs(metrics.ttftMs) },
    { label: 'tokens', value: formatInt(metrics.totalTokens) },
    { label: 'cached', value: formatInt(metrics.cachedTokens) },
    { label: 'cache hit', value: formatPercent(metrics.cacheHitRatio) },
    { label: 'cache key', value: shorten(metrics.promptCacheKey, 42), title: metrics.promptCacheKey }
  ].filter((item) => item.value)
}

function numberFrom(value: unknown): number | null {
  if (value == null || value === '') return null
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

function stringFrom(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function formatMs(value: number | null) {
  if (value == null) return ''
  if (value < 1000) return `${Math.round(value)}ms`
  return `${(value / 1000).toFixed(2)}s`
}

function formatInt(value: number | null) {
  if (value == null) return ''
  return Math.round(value).toLocaleString()
}

function formatPercent(value: number | null) {
  if (value == null) return ''
  return `${Math.round(value * 100)}%`
}

function shorten(value: string, maxLength: number) {
  if (!value) return ''
  if (value.length <= maxLength) return value
  return `${value.slice(0, maxLength - 1)}...`
}
