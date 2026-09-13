import './styles/index.css';

import { route, setFallback, startRouter } from './lib/router';
import { Shell } from './components/Shell';
import { Dashboard } from './screens/dashboard';
import { Episode } from './screens/episode/index';
import { NewEpisode } from './screens/new-episode';
import { CropSetup } from './screens/crop-setup';
import { ClipReview } from './screens/clip-review';
import { LongformReview } from './screens/longform-review';
import { Publish } from './screens/publish';
import { Backup } from './screens/backup';
import { Schedule } from './screens/schedule';
import { Delivery } from './screens/delivery';
import { NotFound } from './screens/not-found';
import { watchEpisode } from './state/episodes';

const root = document.getElementById('app');
if (!root) throw new Error('#app mount point missing');

const { root: shell, main } = Shell();
root.replaceChildren(shell);

function episodeRoute(
  pattern: string,
  handler: (id: string, params: Record<string, string>) => void
): void {
  route(pattern, (params) => {
    watchEpisode(params.id);
    handler(params.id, params);
  });
}

route('/', () => {
  watchEpisode(null);
  Dashboard(main);
});
route('/new', () => {
  watchEpisode(null);
  NewEpisode(main);
});
route('/schedule', () => {
  watchEpisode(null);
  Schedule(main);
});

episodeRoute('/episodes/:id', (id) => Episode(main, id));
episodeRoute('/episodes/:id/longform', (id) => Episode(main, id));
episodeRoute('/episodes/:id/clips', (id) => Episode(main, id));
episodeRoute('/episodes/:id/audio', (id) => Episode(main, id));
episodeRoute('/episodes/:id/metadata', (id) => Episode(main, id));

episodeRoute('/episodes/:id/crop-setup', (id) => CropSetup(main, id));
episodeRoute('/episodes/:id/clips/review/:clipId', (id, { clipId }) =>
  ClipReview(main, id, clipId)
);
episodeRoute('/episodes/:id/clips/review', (id) => ClipReview(main, id));
episodeRoute('/episodes/:id/longform/review', (id) => LongformReview(main, id));
episodeRoute('/episodes/:id/publish', (id) => Publish(main, id));
episodeRoute('/episodes/:id/backup', (id) => Backup(main, id));
episodeRoute('/episodes/:id/delivery', (id) => Delivery(main, id));

setFallback(() => {
  watchEpisode(null);
  NotFound(main);
});

startRouter();
