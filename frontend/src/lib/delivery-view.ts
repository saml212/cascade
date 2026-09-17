import type { DeliveryStatus } from './api';

/** Keep current video review available independently from podcast MP3 state. */
export function showDeliveryVideoSection(
  delivery: DeliveryStatus | null
): delivery is DeliveryStatus {
  return Boolean(
    delivery &&
      (delivery.status === 'ready' || delivery.video_status === 'ready')
  );
}

/** Video preparation still depends on the independently prepared podcast audio. */
export function canPrepareDeliveryVideo(delivery: DeliveryStatus): boolean {
  return delivery.status === 'ready';
}
