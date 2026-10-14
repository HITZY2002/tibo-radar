'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { argumentsFrom, postsFromResponse, mergePosts } = require('./fetch_tweets.cjs');

test('collector accepts bounded, validated arguments without a high-water filter', () => {
  const args = argumentsFrom(['--handle', '@thsottiaux', '--limit', '500', '--since-id', '999']);
  assert.equal(args.handle, 'thsottiaux');
  assert.equal(args.limit, 200);
  assert.throws(() => argumentsFrom(['--handle', '../private']));
});

test('structured original long text replaces truncated or translated DOM content', () => {
  const data = { data: { result: { rest_id: '101',
    legacy: { full_text: 'Short...', truncated: true, created_at: 'Mon Oct 12 10:00:00 +0000 2026' },
    core: { user_results: { result: { legacy: { screen_name: 'thsottiaux' } } } },
    note_tweet: { note_tweet_results: { result: { text: 'The complete original announcement.' } } }
  } } };
  const posts = postsFromResponse(data, 'thsottiaux');
  assert.equal(posts.length, 1);
  assert.equal(posts[0].text, 'The complete original announcement.');
  assert.equal(posts[0].content_complete, true);
});

test('other authors and repost wrappers are not accepted', () => {
  const data = { rest_id: '101', legacy: { full_text: 'Text', retweeted_status_result: {} },
    core: { user_results: { result: { legacy: { screen_name: 'thsottiaux' } } } } };
  assert.equal(postsFromResponse(data, 'thsottiaux').length, 0);
  delete data.legacy.retweeted_status_result;
  assert.equal(postsFromResponse(data, 'another_author').length, 0);
});

test('deduplication retains unseen older posts and prefers complete evidence', () => {
  const post = { id: '101', author: 'thsottiaux', text: 'short', content_complete: false, isRetweet: false };
  const result = mergePosts([post, { ...post, text: 'full text', content_complete: true },
    { ...post, id: '99', content_complete: true }], 'thsottiaux');
  assert.deepEqual(result.map(item => item.id), ['101', '99']);
  assert.equal(result[0].text, 'full text');
});
