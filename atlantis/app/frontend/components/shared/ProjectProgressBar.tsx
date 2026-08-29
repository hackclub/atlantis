const PLACEHOLDER_STEPS = 5
const PLACEHOLDER_COMPLETED = 3

const ProjectProgressBar = () => {
  return (
    <div>
      <h2 className="font-bold text-lg text-dark-brown mb-3">Progress</h2>
      <div className="flex items-center w-full">
        {Array.from({ length: PLACEHOLDER_STEPS }, (_, i) => {
          const completed = i < PLACEHOLDER_COMPLETED
          return (
            <div key={i} className="flex items-center flex-1 first:flex-initial">
              {i > 0 && <div className={`h-1.5 flex-1 ${completed ? 'bg-brown' : 'bg-dark-brown/30'}`} />}
              <div className={`rounded-full shrink-0 ${completed ? 'bg-brown w-6 h-6' : 'bg-dark-brown/30 w-5 h-5'}`} />
            </div>
          )
        })}
      </div>
    </div>
  )
}

export default ProjectProgressBar
