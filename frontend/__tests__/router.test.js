const { getRoute } = require('../utils');

describe('getRoute', () => {
  test('default route is dashboard', () => {
    expect(getRoute('#/')).toEqual({ view: 'dashboard' });
  });

  test('empty hash is dashboard', () => {
    expect(getRoute('')).toEqual({ view: 'dashboard' });
  });

  test('episode detail', () => {
    expect(getRoute('#/episodes/ep_001')).toEqual({
      view: 'episode-detail',
      episodeId: 'ep_001',
      tab: null,
    });
  });

  test('episode detail with tab', () => {
    expect(getRoute('#/episodes/ep_001/clips')).toEqual({
      view: 'episode-detail',
      episodeId: 'ep_001',
      tab: 'clips',
    });
  });

  test('crop setup', () => {
    expect(getRoute('#/episodes/ep_001/crop-setup')).toEqual({
      view: 'crop-setup',
      episodeId: 'ep_001',
    });
  });

  test('clip detail', () => {
    expect(getRoute('#/episodes/ep_001/clips/clip_01')).toEqual({
      view: 'clip-detail',
      episodeId: 'ep_001',
      clipId: 'clip_01',
    });
  });

  test('schedule view', () => {
    expect(getRoute('#/schedule')).toEqual({ view: 'schedule' });
  });

  test('analytics view', () => {
    expect(getRoute('#/analytics')).toEqual({ view: 'analytics' });
  });

  test('unknown route defaults to dashboard', () => {
    expect(getRoute('#/unknown/route')).toEqual({ view: 'dashboard' });
  });
});
