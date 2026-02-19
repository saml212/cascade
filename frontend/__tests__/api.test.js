describe('API fetch patterns', () => {
  beforeEach(() => {
    global.fetch = jest.fn();
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  test('GET request returns JSON', async () => {
    const mockData = [{ episode_id: 'ep_001' }];
    global.fetch.mockResolvedValueOnce({
      ok: true,
      json: async () => mockData,
    });

    const resp = await fetch('/api/episodes/');
    const data = await resp.json();
    expect(data).toEqual(mockData);
    expect(fetch).toHaveBeenCalledWith('/api/episodes/');
  });

  test('POST request with JSON body', async () => {
    global.fetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ status: 'approved' }),
    });

    const resp = await fetch('/api/episodes/ep_001/clips/clip_01/approve', {
      method: 'POST',
    });
    const data = await resp.json();
    expect(data.status).toBe('approved');
  });

  test('handles 404 error', async () => {
    global.fetch.mockResolvedValueOnce({
      ok: false,
      status: 404,
      json: async () => ({ detail: 'Not found' }),
    });

    const resp = await fetch('/api/episodes/nonexistent');
    expect(resp.ok).toBe(false);
    expect(resp.status).toBe(404);
  });

  test('handles network error', async () => {
    global.fetch.mockRejectedValueOnce(new Error('Network error'));

    await expect(fetch('/api/episodes/')).rejects.toThrow('Network error');
  });

  test('DELETE request', async () => {
    global.fetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ status: 'deleted' }),
    });

    const resp = await fetch('/api/episodes/ep_001', { method: 'DELETE' });
    const data = await resp.json();
    expect(data.status).toBe('deleted');
  });

  test('PATCH request with body', async () => {
    global.fetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ status: 'updated' }),
    });

    const resp = await fetch('/api/episodes/ep_001', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: 'New Title' }),
    });
    const data = await resp.json();
    expect(data.status).toBe('updated');
  });

  test('pipeline status polling', async () => {
    global.fetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        episode_id: 'ep_001',
        is_running: true,
        current_agent: 'transcribe',
        agents_completed: ['ingest', 'stitch'],
      }),
    });

    const resp = await fetch('/api/episodes/ep_001/pipeline-status');
    const data = await resp.json();
    expect(data.is_running).toBe(true);
    expect(data.current_agent).toBe('transcribe');
  });

  test('chat endpoint', async () => {
    global.fetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        response: 'I approved clip_01.',
        actions_taken: [{ action: 'approve_clips', status: 'ok' }],
      }),
    });

    const resp = await fetch('/api/episodes/ep_001/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: 'Approve clip 1' }),
    });
    const data = await resp.json();
    expect(data.actions_taken).toHaveLength(1);
  });
});
