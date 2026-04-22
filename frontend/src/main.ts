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
import { Analytics } from './screens/analytics';
import { NotFound } from './screens/not-found';
import { watchEpisode } from './state/episodes';

const root = document.getElementById('app');
if (!root) throw new Error('#app mount point missing');

const { root: shell, main } = Shell();
root.replaceChildren(shell);

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
route('/analytics', () => {
  watchEpisode(null);
  Analytics(main);
});

route('/episodes/:id', ({ id }) => Episode(main, id));
route('/episodes/:id/longform', ({ id }) => Episode(main, id));
route('/episodes/:id/clips', ({ id }) => Episode(main, id));
route('/episodes/:id/audio', ({ id }) => Episode(main, id));
route('/episodes/:id/metadata', ({ id }) => Episode(main, id));

route('/episodes/:id/crop-setup', ({ id }) => CropSetup(main, id));
route('/episodes/:id/clips/review', ({ id }) => ClipReview(main, id));
route('/episodes/:id/longform/review', ({ id }) => LongformReview(main, id));
route('/episodes/:id/publish', ({ id }) => Publish(main, id));
route('/episodes/:id/backup', ({ id }) => Backup(main, id));

setFallback(() => NotFound(main));

startRouter();
