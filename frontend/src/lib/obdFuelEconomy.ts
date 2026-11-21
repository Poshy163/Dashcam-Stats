import type { OBDFuelEconomy } from './api'

function coverage(percent: number): string {
  if (percent > 99 && percent < 100) return '>99%'
  if (percent > 0 && percent < 1) return '<1%'
  return `${Math.round(percent)}%`
}

export function formatFuelEconomy(economy: OBDFuelEconomy): string {
  const parts = []
  if (economy.fuelEconomyCoveragePct != null) {
    parts.push(`${coverage(economy.fuelEconomyCoveragePct)} of recorded distance`)
  }
  if (economy.fuelEconomyTimeCoveragePct != null) {
    parts.push(`${coverage(economy.fuelEconomyTimeCoveragePct)} of recorded time`)
  }
  const ratio = economy.averageFuelConsumptionL100km != null
    ? `${economy.averageFuelConsumptionL100km.toFixed(1)} L/100 km`
    : 'Economy unavailable'
  return parts.length ? `${ratio} · ${parts.join(' · ')}` : ratio
}
