/** Cron's day of week has two numbers for Sunday, 0 and 7. The day picker only
 *  has 0 to 6, so a hand-typed 7 is shown as the Sunday it means. */
export function cronDay(day: number): number {
  return day === 7 ? 0 : day;
}
