// worker/src/index.js — CF Worker for flight-intel job queue.
//
// Endpoints:
//   POST /api/search         enqueue a job, return {job_id}
//   GET  /api/status?job_id  return {stage, progress_pct, log_entry, results, error}
//
// State lives in KV namespace FLIGHT_JOBS. The Mac backend polls KV every 5s for
// queued jobs and writes back status updates.

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const cors = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
    };

    if (request.method === "OPTIONS") {
      return new Response(null, {headers: cors});
    }

    if (url.pathname === "/api/search" && request.method === "POST") {
      const params = await request.json();
      const job_id = crypto.randomUUID().slice(0, 8);
      const job = {
        job_id,
        params,
        stage: "queued",
        created_at: new Date().toISOString(),
        progress_pct: 0,
        log: [],
        results: null,
      };
      await env.FLIGHT_JOBS.put(`job:${job_id}`, JSON.stringify(job), {
        expirationTtl: 86400,  // 1 day
      });
      // Add to queue list (sorted set substitute: append job_id)
      const queue = JSON.parse(await env.FLIGHT_JOBS.get("queue:pending") || "[]");
      queue.push(job_id);
      await env.FLIGHT_JOBS.put("queue:pending", JSON.stringify(queue));
      return new Response(JSON.stringify({job_id}), {
        headers: {"Content-Type": "application/json", ...cors},
      });
    }

    if (url.pathname === "/api/status" && request.method === "GET") {
      const job_id = url.searchParams.get("job_id");
      if (!job_id) return new Response("missing job_id", {status: 400, headers: cors});
      const raw = await env.FLIGHT_JOBS.get(`job:${job_id}`);
      if (!raw) return new Response(JSON.stringify({stage: "not_found"}), {
        status: 404, headers: {"Content-Type": "application/json", ...cors},
      });
      const job = JSON.parse(raw);
      // Return latest log entry only (frontend appends)
      const log_entry = job.log && job.log.length > 0 ? job.log[job.log.length - 1] : null;
      return new Response(JSON.stringify({
        stage: job.stage,
        progress_pct: job.progress_pct || 0,
        log_entry,
        results: job.results,
        route_candidates: job.route_candidates || [],
        fx_rates_brl_per: job.fx_rates_brl_per || null,
        error: job.error,
      }), {headers: {"Content-Type": "application/json", ...cors}});
    }

    // Backend-only: pull next queued job
    if (url.pathname === "/api/_internal/dequeue" && request.method === "POST") {
      const auth = request.headers.get("X-Backend-Auth");
      if (auth !== env.BACKEND_TOKEN) {
        return new Response("forbidden", {status: 403});
      }
      const queue = JSON.parse(await env.FLIGHT_JOBS.get("queue:pending") || "[]");
      if (queue.length === 0) {
        return new Response(JSON.stringify({job: null}), {headers: {"Content-Type": "application/json"}});
      }
      const job_id = queue.shift();
      await env.FLIGHT_JOBS.put("queue:pending", JSON.stringify(queue));
      const job = JSON.parse(await env.FLIGHT_JOBS.get(`job:${job_id}`));
      job.stage = "in_progress";
      await env.FLIGHT_JOBS.put(`job:${job_id}`, JSON.stringify(job));
      return new Response(JSON.stringify({job}), {headers: {"Content-Type": "application/json"}});
    }

    // Backend-only: update job state
    if (url.pathname === "/api/_internal/update" && request.method === "POST") {
      const auth = request.headers.get("X-Backend-Auth");
      if (auth !== env.BACKEND_TOKEN) return new Response("forbidden", {status: 403});
      const update = await request.json();
      const {job_id} = update;
      const raw = await env.FLIGHT_JOBS.get(`job:${job_id}`);
      if (!raw) return new Response("not found", {status: 404});
      const job = JSON.parse(raw);
      Object.assign(job, update);
      if (update.log_append) {
        job.log = job.log || [];
        job.log.push(update.log_append);
      }
      await env.FLIGHT_JOBS.put(`job:${job_id}`, JSON.stringify(job), {expirationTtl: 86400});
      return new Response("ok");
    }

    return new Response("flight-intel worker. endpoints: POST /api/search, GET /api/status", {headers: cors});
  },
};
