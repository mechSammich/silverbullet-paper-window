function random_task(myTag)
  local _task_list = query[[from t = index.tag(myTag) where not t.done]]
  if #_task_list == 0 then
    return nil
  end
  return _task_list[math.random(#_task_list)]
end

function write_daily_random_task(myTag)
  local _task_page = "external/" .. myTag
  local _ext_meta = space.getPageMeta(_task_page)
  local _ext_date = _ext_meta.lastModified:match("(%d%d%d%d%-%d%d%-%d%d)")

  -- Page meta can lag a fresh write or skip frontmatter keys; parse the body.
  local _task_ref = nil
  if _ext_date == date.today() then
    local _body = space.readPage(_task_page)
    local _fm = _body and _body:match("^%-%-%-\n(.-)\n%-%-%-\n")
    _task_ref = _fm and _fm:match("task_ref:%s*([^%s]+)")
  end

  -- Same day and a recorded task: keep it unless it was completed elsewhere.
  if _task_ref then
    local _cur = query[[from t = index.tasks() where t.ref == _task_ref]]
    if #_cur > 0 and not _cur[1].done then
      return query[[from t = index.tasks() where t.ref == _task_ref select templates.taskItem(t)]]
    end
  end

  local _task = random_task(myTag)
  if not _task then
    return nil
  end
  local _ref = _task.ref
  local _body = "---\n"
    .. "task_ref: " .. _ref .. "\n"
    .. "task_page: " .. _task.page .. "\n"
    .. "---\n"
    .. "- [ ] " .. _task.name .. "\n"
  space.writePage(_task_page, _body)
  return query[[from t = index.tasks() where t.ref == _ref select templates.taskItem(t)]]
end