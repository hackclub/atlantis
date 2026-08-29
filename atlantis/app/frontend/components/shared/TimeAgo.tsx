import { useState, useEffect } from 'react'

function countLeapYears(fromYear: number, toYear: number): number {
  if (fromYear > toYear) return 0
  const leapYearsUpTo = (year: number) => Math.floor(year / 4) - Math.floor(year / 100) + Math.floor(year / 400)
  return leapYearsUpTo(toYear) - leapYearsUpTo(fromYear - 1)
}

function distanceInWords(fromTime: Date, toTime: Date): string {
  if (fromTime > toTime) [fromTime, toTime] = [toTime, fromTime]

  const distanceInSeconds = Math.round((toTime.getTime() - fromTime.getTime()) / 1000)
  const distanceInMinutes = Math.round(distanceInSeconds / 60)

  if (distanceInMinutes <= 1) return distanceInMinutes === 0 ? 'less than a minute' : '1 minute'
  if (distanceInMinutes < 45) return `${distanceInMinutes} minutes`
  if (distanceInMinutes < 90) return 'about 1 hour'
  if (distanceInMinutes < 1440) {
    const hours = Math.round(distanceInMinutes / 60)
    return `about ${hours} ${hours === 1 ? 'hour' : 'hours'}`
  }
  if (distanceInMinutes < 2520) return '1 day'
  if (distanceInMinutes < 43200) {
    const days = Math.round(distanceInMinutes / 1440)
    return `${days} ${days === 1 ? 'day' : 'days'}`
  }
  if (distanceInMinutes < 86400) {
    const months = Math.round(distanceInMinutes / 43200)
    return `about ${months} ${months === 1 ? 'month' : 'months'}`
  }
  if (distanceInMinutes < 525600) {
    const months = Math.round(distanceInMinutes / 43200)
    return `${months} ${months === 1 ? 'month' : 'months'}`
  }

  const MINUTES_IN_YEAR = 525600
  const MINUTES_IN_QUARTER_YEAR = 131400
  const MINUTES_IN_THREE_QUARTERS_YEAR = 394200

  let fromYear = fromTime.getFullYear()
  if (fromTime.getMonth() >= 2) fromYear++
  let toYear = toTime.getFullYear()
  if (toTime.getMonth() < 2) toYear--

  const minuteOffsetForLeapYear = countLeapYears(fromYear, toYear) * 1440
  const minutesWithOffset = distanceInMinutes - minuteOffsetForLeapYear
  const distanceInYears = Math.floor(minutesWithOffset / MINUTES_IN_YEAR)
  const remainder = minutesWithOffset % MINUTES_IN_YEAR

  let years: number, prefix: string
  if (remainder < MINUTES_IN_QUARTER_YEAR) {
    prefix = 'about'
    years = distanceInYears
  } else if (remainder < MINUTES_IN_THREE_QUARTERS_YEAR) {
    prefix = 'over'
    years = distanceInYears
  } else {
    prefix = 'almost'
    years = distanceInYears + 1
  }

  return `${prefix} ${years} ${years === 1 ? 'year' : 'years'}`
}

export default function TimeAgo({ datetime, suffix = 'ago' }: { datetime: string; suffix?: string }) {
  const [text, setText] = useState(() => distanceInWords(new Date(datetime), new Date()))

  useEffect(() => {
    const update = () => setText(distanceInWords(new Date(datetime), new Date()))
    update()
    const interval = setInterval(update, 60000)
    return () => clearInterval(interval)
  }, [datetime])

  return (
    <time dateTime={datetime} title={new Date(datetime).toLocaleString()}>
      {text}
      {suffix ? ` ${suffix}` : ''}
    </time>
  )
}
