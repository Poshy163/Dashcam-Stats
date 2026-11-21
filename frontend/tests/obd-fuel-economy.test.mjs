import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { test } from 'node:test'
import { transform } from 'esbuild'

const source = await readFile(new URL('../src/lib/obdFuelEconomy.ts', import.meta.url), 'utf8')
const { code } = await transform(source, { loader: 'ts', format: 'esm' })
const { formatFuelEconomy } = await import(`data:text/javascript,${encodeURIComponent(code)}`)

test('fuel economy distinguishes uncovered idle time, zero fuel, and missing observations', () => {
  assert.equal(formatFuelEconomy({
    averageFuelConsumptionL100km: 10, fuelEconomyCoveragePct: 100, fuelEconomyTimeCoveragePct: 66.67,
  }), '10.0 L/100 km · 100% of recorded distance · 67% of recorded time')
  assert.equal(formatFuelEconomy({
    averageFuelConsumptionL100km: 0, fuelEconomyCoveragePct: 99.9, fuelEconomyTimeCoveragePct: 0.4,
  }), '0.0 L/100 km · >99% of recorded distance · <1% of recorded time')
  assert.equal(formatFuelEconomy({ fuelEconomyTimeCoveragePct: 100 }), 'Economy unavailable · 100% of recorded time')
  assert.equal(formatFuelEconomy({}), 'Economy unavailable')
})
